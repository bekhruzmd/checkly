"""
Presence check endpoints — idle-triggered desk confirmations.

Flow:
  1. Desktop idle agent detects keyboard/mouse idle > threshold
  2. Agent POSTs idle_start to /presence/idle-events
  3. Backend creates a presence_check row, sends Telegram prompt to worker
  4. Worker replies with a selfie via Telegram
  5. telegram_bot.py downloads photo and POSTs to /presence/checks/{id}/respond
  6. Backend runs ArcFace match; updates check result (passed/failed)
  7. Manager can POST /presence/checks/{id}/excuse for failed checks (with reason)

A background job (scheduled in main.py) marks pending checks as failed when
their response window expires.

KNOWN RISK — night-shift selfies:
  ArcFace (buffalo_l) accuracy degrades under low-light conditions typical of
  night-shift desk selfies. The existing liveness sharpness heuristic may also
  produce false negatives for flash-lit photos. Do NOT trust night-shift failure
  data until you've tested a real sample and confirmed confidence distributions.
  See shift_monitoring_config.response_window_seconds for per-shift tuning.

KNOWN RISK — Telegram JPEG compression:
  Photos sent via Telegram are JPEG-compressed before delivery. This can reduce
  cosine similarity by 0.02–0.05 vs. direct-upload check-ins. Monitor confidence
  score distributions for Telegram-sourced responses separately.
"""

import asyncpg
from datetime import datetime, time as dtime
from fastapi import APIRouter, Form, HTTPException, UploadFile
from pydantic import BaseModel

import face_match
import telegram_bot
from utils import compute_row_hash

router = APIRouter(prefix="/presence", tags=["presence"])

# Injected by main.py after pool creation.
pool: asyncpg.Pool | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_within_shift(start: dtime, end: dtime, now_time: dtime) -> bool:
    if start <= end:
        return start <= now_time <= end
    # Crosses midnight (e.g. night shift 21:00–05:00)
    return now_time >= start or now_time <= end


async def _get_active_schedule(conn, worker_id: int, for_date):
    """Return the joined schedule+shift row active on for_date, or None."""
    return await conn.fetchrow(
        """
        SELECT sc.id AS schedule_id, sh.id AS shift_id, sh.name AS shift_name,
               sh.start_time, sh.end_time, sh.grace_period_min, sh.fee_per_min,
               smc.idle_threshold_seconds, smc.response_window_seconds,
               smc.enabled AS monitoring_enabled
        FROM schedules sc
        JOIN shifts sh ON sh.id = sc.shift_id
        LEFT JOIN shift_monitoring_config smc ON smc.shift_id = sh.id
        WHERE sc.worker_id = $1
          AND sc.effective_from <= $2
          AND (sc.effective_to IS NULL OR sc.effective_to >= $2)
        ORDER BY sc.effective_from DESC
        LIMIT 1
        """,
        worker_id, for_date,
    )


async def _get_last_presence_hash(conn) -> str | None:
    row = await conn.fetchrow(
        "SELECT row_hash FROM presence_checks ORDER BY id DESC LIMIT 1"
    )
    return row["row_hash"] if row else None


async def _expire_pending_checks(conn) -> int:
    """Mark pending checks whose window has expired as failed. Returns count updated."""
    result = await conn.execute(
        """
        UPDATE presence_checks
        SET result = 'failed'
        WHERE result = 'pending'
          AND triggered_at + (INTERVAL '1 second' * window_seconds) < now()
        """
    )
    n = int(result.split()[-1])
    return n


# ── Scheduled job (called from main.py via APScheduler) ──────────────────────

async def expire_pending_checks() -> None:
    """Run every 30 s via the scheduler to flip timed-out pending checks to failed."""
    async with pool.acquire() as conn:
        n = await _expire_pending_checks(conn)
        if n:
            import logging
            logging.getLogger(__name__).info("Expired %d presence check(s)", n)


# ── Endpoints ─────────────────────────────────────────────────────────────────

class IdleEvent(BaseModel):
    worker_id: int
    event: str  # "idle_start" | "idle_end"


@router.post("/idle-events", status_code=202)
async def receive_idle_event(body: IdleEvent):
    """
    Receives idle_start / idle_end from the desktop idle agent.
    Only idle_start creates a presence check; idle_end is acknowledged
    but does not automatically pass the check — the worker must send a selfie.
    """
    if body.event not in ("idle_start", "idle_end"):
        raise HTTPException(400, "event must be 'idle_start' or 'idle_end'")

    if body.event == "idle_end":
        return {"status": "acknowledged"}

    # idle_start path
    async with pool.acquire() as conn:
        worker = await conn.fetchrow(
            "SELECT id, full_name, telegram_chat_id FROM workers WHERE id = $1 AND active = TRUE",
            body.worker_id,
        )
        if not worker:
            raise HTTPException(404, "Worker not found or inactive")

        now = datetime.now().astimezone()
        schedule = await _get_active_schedule(conn, body.worker_id, now.date())

        if not schedule:
            return {"status": "no_schedule", "detail": "Worker has no active schedule today"}

        if not schedule["monitoring_enabled"]:
            return {"status": "monitoring_disabled"}

        if not _is_within_shift(schedule["start_time"], schedule["end_time"], now.time()):
            return {"status": "outside_shift_window"}

        window_sec = schedule["response_window_seconds"] or 180

        # Idempotency: don't stack checks — skip if a pending check exists within the window.
        existing = await conn.fetchrow(
            """
            SELECT id FROM presence_checks
            WHERE worker_id = $1
              AND result = 'pending'
              AND triggered_at > now() - (INTERVAL '1 second' * $2)
            """,
            body.worker_id, window_sec,
        )
        if existing:
            return {"status": "already_pending", "check_id": existing["id"]}

        # Create the presence check row.
        prev_hash = await _get_last_presence_hash(conn)
        payload   = {
            "worker_id":      body.worker_id,
            "triggered_at":   now.isoformat(),
            "window_seconds": window_sec,
        }
        row_hash = compute_row_hash(prev_hash, payload)

        check_id = await conn.fetchval(
            """
            INSERT INTO presence_checks
              (worker_id, triggered_at, window_seconds, result, prev_hash, row_hash)
            VALUES ($1, $2, $3, 'pending', $4, $5)
            RETURNING id
            """,
            body.worker_id, now, window_sec, prev_hash, row_hash,
        )

    # Notify worker via Telegram (outside the DB transaction).
    if worker["telegram_chat_id"]:
        telegram_bot.register_pending(worker["telegram_chat_id"], check_id)
        telegram_bot.send_presence_prompt(
            worker["telegram_chat_id"], check_id, window_sec
        )

    return {"status": "check_created", "check_id": check_id}


@router.post("/checks/{check_id}/respond")
async def respond_to_presence_check(check_id: int, photo: UploadFile):
    """
    Called by the Telegram bot after the worker sends a selfie.
    Runs ArcFace match and records the result.
    """
    photo_bytes = await photo.read()

    async with pool.acquire() as conn:
        check = await conn.fetchrow(
            "SELECT * FROM presence_checks WHERE id = $1",
            check_id,
        )
        if not check:
            raise HTTPException(404, "Presence check not found")
        if check["result"] != "pending":
            raise HTTPException(409, f"Check already resolved: {check['result']}")

        # Verify window hasn't expired.
        from datetime import timezone
        triggered = check["triggered_at"]
        if triggered.tzinfo is None:
            triggered = triggered.replace(tzinfo=timezone.utc)
        now = datetime.now().astimezone()
        elapsed = (now - triggered).total_seconds()
        if elapsed > check["window_seconds"]:
            await conn.execute(
                "UPDATE presence_checks SET result = 'failed' WHERE id = $1",
                check_id,
            )
            del photo_bytes
            raise HTTPException(410, "Response window has expired — check marked failed")

        # Fetch worker's enrollment embedding.
        worker = await conn.fetchrow(
            "SELECT face_embedding FROM workers WHERE id = $1",
            check["worker_id"],
        )
        enrolled = [(check["worker_id"], worker["face_embedding"])]

        liveness_ok   = face_match.check_liveness(photo_bytes)
        matched_id, confidence, status = face_match.match_face(photo_bytes, enrolled)
        del photo_bytes

        # A liveness failure downgrades to failed even on a face match.
        if not liveness_ok or matched_id is None:
            result = "failed"
        elif status == "accepted":
            result = "passed"
        else:
            # manual_review threshold: treat as failed for presence checks —
            # the worker should respond clearly; borderline selfies in poor
            # lighting are common on night shift (see module-level risk notes).
            result = "failed"

        await conn.execute(
            """
            UPDATE presence_checks
            SET result = $1, confidence = $2, responded_at = $3
            WHERE id = $4
            """,
            result, confidence, now, check_id,
        )

    return {"check_id": check_id, "result": result, "confidence": confidence}


class ExcuseBody(BaseModel):
    manager_id: int
    reason: str


@router.post("/checks/{check_id}/excuse")
async def excuse_presence_check(check_id: int, body: ExcuseBody):
    """
    Manager excuses a failed presence check with a required reason.
    Only works on result='failed'; cannot excuse pending, passed, or already-excused checks.
    """
    if not body.reason or not body.reason.strip():
        raise HTTPException(400, "reason is required and must not be empty")

    async with pool.acquire() as conn:
        check = await conn.fetchrow(
            "SELECT result, worker_id FROM presence_checks WHERE id = $1",
            check_id,
        )
        if not check:
            raise HTTPException(404, "Presence check not found")
        if check["result"] != "failed":
            raise HTTPException(
                409,
                f"Can only excuse failed checks — current result is '{check['result']}'",
            )

        manager = await conn.fetchrow(
            "SELECT id, full_name FROM managers WHERE id = $1 AND active = TRUE",
            body.manager_id,
        )
        if not manager:
            raise HTTPException(404, "Manager not found or inactive")

        now = datetime.now().astimezone()
        await conn.execute(
            """
            UPDATE presence_checks
            SET result = 'excused', excused_by = $1, excuse_reason = $2, excused_at = $3
            WHERE id = $4
            """,
            body.manager_id, body.reason.strip(), now, check_id,
        )

    return {
        "check_id":  check_id,
        "result":    "excused",
        "excused_by": manager["full_name"],
    }


# ── Agent helper endpoint ─────────────────────────────────────────────────────

@router.get("/workers/{worker_id}/shift-window")
async def get_shift_window(worker_id: int):
    """
    Called by the idle agent on startup to learn the worker's shift window
    and the configured idle threshold for today. Returns monitoring_enabled=false
    if no active schedule exists for today.
    """
    async with pool.acquire() as conn:
        now      = datetime.now().astimezone()
        schedule = await _get_active_schedule(conn, worker_id, now.date())

        if not schedule:
            return {"worker_id": worker_id, "monitoring_enabled": False}

        start: dtime = schedule["start_time"]
        end:   dtime = schedule["end_time"]

        return {
            "worker_id":              worker_id,
            "shift_name":             schedule["shift_name"],
            "start_time":             start.strftime("%H:%M:%S"),
            "end_time":               end.strftime("%H:%M:%S"),
            "crosses_midnight":       start > end,
            "idle_threshold_seconds": schedule["idle_threshold_seconds"] or 360,
            "response_window_seconds":schedule["response_window_seconds"] or 180,
            "monitoring_enabled":     bool(schedule["monitoring_enabled"]),
            "within_window":          _is_within_shift(start, end, now.time()),
        }


# ── Manager excuse-rate view ──────────────────────────────────────────────────

@router.get("/managers/excuse-rates")
async def excuse_rates():
    """
    Returns per-manager excuse statistics.
    Rate = excuses_given / total_failures_in_system.
    A high rate means this manager excuses an unusually large share of all failures.
    No alert threshold is enforced — this is an observation tool.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                m.id,
                m.full_name,
                COUNT(pc.id) FILTER (WHERE pc.excused_by = m.id)                       AS excuses_given,
                COUNT(pc.id) FILTER (WHERE pc.result IN ('failed', 'excused'))          AS total_failures,
                ROUND(
                    COUNT(pc.id) FILTER (WHERE pc.excused_by = m.id)::NUMERIC /
                    NULLIF(COUNT(pc.id) FILTER (WHERE pc.result IN ('failed','excused')), 0)
                    * 100, 1
                )                                                                        AS excuse_rate_pct
            FROM managers m
            CROSS JOIN presence_checks pc
            WHERE m.active = TRUE
            GROUP BY m.id, m.full_name
            ORDER BY excuse_rate_pct DESC NULLS LAST
            """
        )
    return [dict(r) for r in rows]
