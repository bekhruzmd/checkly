"""
Desk-presence event ingestion and leave-summary endpoints (backend).

The edge device (running desk_presence.py on the Tashkent office LAN)
POSTs occupancy events here over Tailscale. This router:
  1. Writes each event to desk_presence_events (append-only, hash-chained).
  2. Runs AwayStateMachine transitions.
  3. On UNAUTHORIZED leave: upserts shift_leave_summary and sends a
     Telegram notification to the worker.
  4. On AUTHORIZED leave: increments authorized_break_count only.

fee_applied is always written as FALSE — a separate dispute-review step
must happen before any deduction is finalized. This is by design.
"""

import logging
import os
from datetime import datetime, date
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

import telegram_bot
from desk_presence_state import AwayRecord, AwayStateMachine, LeaveOutcome, ResolvedLeave
from utils import compute_row_hash

log = logging.getLogger(__name__)
router = APIRouter(prefix="/desk-presence", tags=["desk-presence"])

# Injected by main.py after pool creation.
pool: asyncpg.Pool | None = None

# Singleton — all in-flight away state lives here.
# Restored from today's DB events at startup (see restore_away_state()).
_sm = AwayStateMachine()


# ── Auth ──────────────────────────────────────────────────────────────────────

async def _require_edge_token(authorization: str = Header(...)) -> None:
    """Shared-secret guard for edge-device endpoints."""
    secret = os.environ.get("EDGE_SECRET", "")
    if not secret:
        raise HTTPException(503, "EDGE_SECRET not configured on backend")
    if authorization != f"Bearer {secret}":
        raise HTTPException(401, "Invalid edge token")


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_zone(conn, desk_zone_id: int):
    return await conn.fetchrow(
        "SELECT * FROM desk_zones WHERE id = $1 AND active = TRUE",
        desk_zone_id,
    )


async def _last_presence_event_hash(conn) -> str | None:
    row = await conn.fetchrow(
        "SELECT event_hash FROM desk_presence_events ORDER BY id DESC LIMIT 1"
    )
    return row["event_hash"] if row else None


async def _write_presence_event(
    conn,
    desk_zone_id: int,
    worker_id: int | None,
    event_type: str,
    event_ts: datetime,
    confidence: float | None,
) -> int:
    prev_hash = await _last_presence_event_hash(conn)
    payload = {
        "desk_zone_id": desk_zone_id,
        "worker_id":    worker_id,
        "event_type":   event_type,
        "event_ts":     event_ts.isoformat(),
        "confidence":   confidence,
    }
    event_hash = compute_row_hash(prev_hash, payload)
    return await conn.fetchval(
        """
        INSERT INTO desk_presence_events
          (desk_zone_id, worker_id, event_type, event_ts, confidence,
           prev_hash, event_hash)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        desk_zone_id, worker_id, event_type, event_ts,
        confidence, prev_hash, event_hash,
    )


async def _upsert_leave_summary(
    conn,
    worker_id: int,
    summary_date: date,
    desk_zone_id: int,
    is_unauthorized: bool,
    duration_seconds: float,
    threshold: int,
) -> dict:
    """Atomically increment the appropriate counter. Returns updated row fields."""
    if is_unauthorized:
        row = await conn.fetchrow(
            """
            INSERT INTO shift_leave_summary
              (worker_id, summary_date, desk_zone_id, leave_count,
               authorized_break_count, total_leave_seconds, threshold_at_time)
            VALUES ($1, $2, $3, 1, 0, $4, $5)
            ON CONFLICT (worker_id, summary_date, desk_zone_id) DO UPDATE SET
              leave_count = shift_leave_summary.leave_count + 1,
              total_leave_seconds =
                shift_leave_summary.total_leave_seconds + EXCLUDED.total_leave_seconds
            RETURNING leave_count, threshold_at_time
            """,
            worker_id, summary_date, desk_zone_id, int(duration_seconds), threshold,
        )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO shift_leave_summary
              (worker_id, summary_date, desk_zone_id, leave_count,
               authorized_break_count, total_leave_seconds, threshold_at_time)
            VALUES ($1, $2, $3, 0, 1, $4, $5)
            ON CONFLICT (worker_id, summary_date, desk_zone_id) DO UPDATE SET
              authorized_break_count = shift_leave_summary.authorized_break_count + 1,
              total_leave_seconds =
                shift_leave_summary.total_leave_seconds + EXCLUDED.total_leave_seconds
            RETURNING leave_count, threshold_at_time
            """,
            worker_id, summary_date, desk_zone_id, int(duration_seconds), threshold,
        )
    return dict(row)


# ── Startup reconstruction ────────────────────────────────────────────────────

async def restore_away_state() -> None:
    """
    Reconstruct open AwayRecords from today's desk_presence_events.

    Called once at FastAPI startup. A worker is "still away" if their most
    recent desk zone event today was occupied_end with no subsequent
    occupied_start in the same zone.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH ranked AS (
                SELECT
                    dpe.worker_id,
                    dpe.desk_zone_id,
                    dpe.event_type,
                    dpe.event_ts,
                    ROW_NUMBER() OVER (
                        PARTITION BY dpe.worker_id
                        ORDER BY dpe.event_ts DESC
                    ) AS rn
                FROM desk_presence_events dpe
                JOIN desk_zones dz ON dz.id = dpe.desk_zone_id
                WHERE dpe.event_ts::date = CURRENT_DATE
                  AND dpe.worker_id IS NOT NULL
                  AND dz.zone_type = 'desk'
            )
            SELECT r.worker_id, r.desk_zone_id, r.event_ts, dz.min_leave_seconds
            FROM ranked r
            JOIN desk_zones dz ON dz.id = r.desk_zone_id
            WHERE r.rn = 1 AND r.event_type = 'occupied_end'
            """,
        )
    for row in rows:
        _sm.restore_record(
            AwayRecord(
                worker_id=row["worker_id"],
                origin_zone_id=row["desk_zone_id"],
                start_ts=row["event_ts"],
                min_leave_seconds=row["min_leave_seconds"],
            )
        )
    if rows:
        log.info("Restored %d open away record(s) from today's DB state", len(rows))


# ── Event ingestion ───────────────────────────────────────────────────────────

class ZoneEvent(BaseModel):
    desk_zone_id: int
    event_type: str                    # "occupied_start" | "occupied_end"
    worker_id: Optional[int] = None   # None when face match failed
    confidence: Optional[float] = None


@router.post("/events", status_code=201, dependencies=[Depends(_require_edge_token)])
async def ingest_zone_event(body: ZoneEvent):
    """
    Receive an occupancy event from the edge device.

    Server timestamp is authoritative — the edge device clock is ignored
    (consistent with the check-in trust model).
    """
    if body.event_type not in ("occupied_start", "occupied_end"):
        raise HTTPException(400, "event_type must be 'occupied_start' or 'occupied_end'")

    now = datetime.now().astimezone()

    # Variables populated inside the DB block, used for Telegram after closing.
    telegram_chat_id: int | None = None
    leave_count: int | None = None
    threshold: int | None = None
    resolved: ResolvedLeave | None = None

    async with pool.acquire() as conn:
        zone = await _get_zone(conn, body.desk_zone_id)
        if not zone:
            raise HTTPException(404, f"Zone {body.desk_zone_id} not found or inactive")

        event_id = await _write_presence_event(
            conn,
            desk_zone_id=body.desk_zone_id,
            worker_id=body.worker_id,
            event_type=body.event_type,
            event_ts=now,
            confidence=body.confidence,
        )

        # State machine — only meaningful when worker identity is confirmed.
        if body.worker_id is not None:
            zone_type = zone["zone_type"]

            if zone_type == "desk" and body.event_type == "occupied_end":
                _sm.desk_vacated(
                    worker_id=body.worker_id,
                    zone_id=body.desk_zone_id,
                    ts=now,
                    min_leave_seconds=zone["min_leave_seconds"],
                )

            elif zone_type == "safe_zone" and body.event_type == "occupied_start":
                _sm.safe_zone_entered(
                    worker_id=body.worker_id,
                    safe_zone_id=body.desk_zone_id,
                    ts=now,
                )

            elif zone_type == "desk" and body.event_type == "occupied_start":
                resolved = _sm.desk_returned(
                    worker_id=body.worker_id,
                    zone_id=body.desk_zone_id,
                    ts=now,
                )

                if resolved and resolved.outcome != LeaveOutcome.NOISE:
                    is_unauth = resolved.outcome == LeaveOutcome.UNAUTHORIZED
                    origin_zone = await _get_zone(conn, resolved.origin_zone_id)
                    zone_threshold = (
                        origin_zone["leave_threshold"]
                        if origin_zone
                        else zone["leave_threshold"]
                    )

                    summary = await _upsert_leave_summary(
                        conn,
                        worker_id=resolved.worker_id,
                        summary_date=now.date(),
                        desk_zone_id=resolved.origin_zone_id,
                        is_unauthorized=is_unauth,
                        duration_seconds=resolved.duration_seconds,
                        threshold=zone_threshold,
                    )
                    leave_count = summary["leave_count"]
                    threshold = summary["threshold_at_time"]

                    if is_unauth:
                        worker_row = await conn.fetchrow(
                            "SELECT telegram_chat_id FROM workers WHERE id = $1",
                            resolved.worker_id,
                        )
                        if worker_row:
                            telegram_chat_id = worker_row["telegram_chat_id"]

    # Telegram — outside DB transaction, best-effort, never raises.
    if resolved and resolved.outcome == LeaveOutcome.UNAUTHORIZED and telegram_chat_id:
        try:
            telegram_bot.send_unauthorized_leave_notification(
                telegram_chat_id, leave_count, threshold
            )
        except Exception:
            log.exception(
                "Telegram leave notification failed for worker %d", body.worker_id
            )

    return {
        "event_id":     event_id,
        "zone_type":    zone["zone_type"],
        "event_type":   body.event_type,
        "resolved_leave": (
            {
                "outcome":          resolved.outcome,
                "duration_seconds": resolved.duration_seconds,
            }
            if resolved
            else None
        ),
    }


# ── Worker embeddings (for edge device) ──────────────────────────────────────

@router.get("/workers/embeddings", dependencies=[Depends(_require_edge_token)])
async def get_worker_embeddings():
    """
    Serve active workers' face embeddings to the edge device.

    The edge device fetches this on startup (and hourly) to keep its local
    embedding cache in sync. Raw embeddings are only reachable via Tailscale
    and never cross the public internet.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, face_embedding FROM workers WHERE active = TRUE"
        )
    return [
        {"worker_id": r["id"], "face_embedding": list(r["face_embedding"])}
        for r in rows
    ]


# ── Leave-summary endpoints ───────────────────────────────────────────────────

@router.get("/leave-summary/{worker_id}")
async def get_worker_leave_summary(worker_id: int, day: date | None = None):
    """
    Worker-facing view of their own leave timestamps and counts for a given day.

    This is the record that makes an automated deduction defensible if
    challenged — the dispute flow starts here. fee_applied is kept separate
    so a human review can intervene before any deduction is finalized.
    """
    day = day or date.today()
    async with pool.acquire() as conn:
        summary = await conn.fetchrow(
            """
            SELECT leave_count, authorized_break_count, total_leave_seconds,
                   threshold_at_time, fee_applied, disputed, dispute_note
            FROM shift_leave_summary
            WHERE worker_id = $1 AND summary_date = $2
            """,
            worker_id, day,
        )
        events = await conn.fetch(
            """
            SELECT dpe.id, dpe.event_type, dpe.event_ts, dpe.confidence,
                   dz.desk_label, dz.zone_type
            FROM desk_presence_events dpe
            JOIN desk_zones dz ON dz.id = dpe.desk_zone_id
            WHERE dpe.worker_id = $1
              AND dpe.event_ts::date = $2
            ORDER BY dpe.event_ts
            """,
            worker_id, day,
        )
    return {
        "worker_id": worker_id,
        "date":      day.isoformat(),
        "summary":   dict(summary) if summary else None,
        "events":    [dict(e) for e in events],
    }


@router.get("/leave-summary")
async def get_all_leave_summaries(day: date | None = None):
    """Admin view: all workers' leave summaries for a given day."""
    day = day or date.today()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT w.full_name, w.shift, sls.*
            FROM shift_leave_summary sls
            JOIN workers w ON w.id = sls.worker_id
            WHERE sls.summary_date = $1
            ORDER BY sls.leave_count DESC, w.full_name
            """,
            day,
        )
    return [dict(r) for r in rows]


class DisputeBody(BaseModel):
    note: str


@router.post("/leave-summary/{worker_id}/dispute")
async def dispute_leave_summary(worker_id: int, body: DisputeBody, day: date | None = None):
    """
    Worker flags a leave summary as disputed with a required note.
    Marks disputed=TRUE so the summary is excluded from automatic fee finalization
    until a manager reviews it.
    """
    if not body.note or not body.note.strip():
        raise HTTPException(400, "note is required")
    day = day or date.today()
    async with pool.acquire() as conn:
        updated = await conn.execute(
            """
            UPDATE shift_leave_summary
            SET disputed = TRUE, dispute_note = $3
            WHERE worker_id = $1 AND summary_date = $2
              AND fee_applied = FALSE
            """,
            worker_id, day, body.note.strip(),
        )
    if updated == "UPDATE 0":
        raise HTTPException(
            404,
            "No leave summary found for this worker/day, or fee already applied.",
        )
    return {"status": "disputed", "worker_id": worker_id, "date": day.isoformat()}
