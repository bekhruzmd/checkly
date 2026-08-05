"""
Attendance MVP API.

Endpoints:
  POST /workers/enroll              -- one-time face enrollment for a new worker
  POST /attendance/check-in         -- worker submits a selfie + GPS coords
  GET  /attendance/sheet            -- attendance log for admin view
  GET  /attendance/daily-summary    -- aggregated daily summary per shift

  /presence/*                       -- presence check endpoints (see presence.py)

No worker photos are stored anywhere -- not on disk, not in the DB.
Photos exist only in request memory for the duration of the call.
"""

import asyncio
import logging
import math
import os
from datetime import datetime, date, time as dtime

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, UploadFile, Form, HTTPException
from pydantic import BaseModel

import aggregation
import desk_presence_api
import face_match
import presence
import telegram_bot
from utils import compute_row_hash

log = logging.getLogger(__name__)

app = FastAPI(title="Attendance MVP")
app.include_router(presence.router)
app.include_router(desk_presence_api.router)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://user:password@localhost:5432/attendance"
)
pool: asyncpg.Pool | None = None
_scheduler = AsyncIOScheduler()


@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

    # Share pool with sub-modules.
    presence.pool          = pool
    desk_presence_api.pool = pool
    aggregation.DATABASE_URL = DATABASE_URL

    # Restore desk-presence state from today's DB events (after pool is set).
    await desk_presence_api.restore_away_state()

    # Configure Telegram bot.
    telegram_bot.configure(
        token    = os.environ.get("TELEGRAM_TOKEN", ""),
        api_base = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000"),
    )
    telegram_bot.start_polling()

    # Schedule presence-check expiry every 30 s.
    # AsyncIOScheduler awaits async job functions directly in the event loop.
    _scheduler.add_job(
        presence.expire_pending_checks,
        trigger="interval",
        seconds=30,
        id="expire_presence_checks",
    )
    # Schedule nightly daily_summary aggregation at 05:30.
    _scheduler.add_job(
        lambda: aggregation.run_for_yesterday(pool),
        trigger="cron",
        hour=5,
        minute=30,
        id="nightly_aggregation",
    )
    _scheduler.start()
    log.info("Startup complete")


@app.on_event("shutdown")
async def shutdown():
    _scheduler.shutdown(wait=False)
    if pool:
        await pool.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def haversine_meters(lat1, lon1, lat2, lon2) -> float:
    R = 6371000
    p1, p2   = math.radians(lat1), math.radians(lat2)
    dphi     = math.radians(lat2 - lat1)
    dlambda  = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


async def get_last_hash(conn) -> str | None:
    row = await conn.fetchrow(
        "SELECT row_hash FROM attendance_events ORDER BY id DESC LIMIT 1"
    )
    return row["row_hash"] if row else None


async def get_office_location(conn):
    return await conn.fetchrow("SELECT * FROM office_locations LIMIT 1")


async def get_active_schedule(conn, worker_id: int, for_date: date):
    """
    Returns the shift config active for this worker on for_date,
    joined from schedules → shifts. Falls back to workers.shift if
    no schedule row exists (supports workers enrolled before the
    schedules table was introduced).
    """
    row = await conn.fetchrow(
        """
        SELECT sh.name AS shift_name, sh.start_time, sh.end_time,
               sh.grace_period_min, sh.fee_per_min
        FROM schedules sc
        JOIN shifts sh ON sh.id = sc.shift_id
        WHERE sc.worker_id = $1
          AND sc.effective_from <= $2
          AND (sc.effective_to IS NULL OR sc.effective_to >= $2)
        ORDER BY sc.effective_from DESC
        LIMIT 1
        """,
        worker_id, for_date,
    )
    if row:
        return row

    # Fallback: use the shift stored directly on the workers row.
    worker = await conn.fetchrow("SELECT shift FROM workers WHERE id = $1", worker_id)
    if not worker:
        return None
    return await conn.fetchrow(
        "SELECT name AS shift_name, start_time, end_time, grace_period_min, fee_per_min "
        "FROM shifts WHERE name = $1",
        worker["shift"],
    )


def compute_lateness(shift_start: dtime, now: datetime, grace_min: int) -> int:
    scheduled    = datetime.combine(now.date(), shift_start, tzinfo=now.tzinfo)
    late_seconds = (now - scheduled).total_seconds()
    return max(0, int(late_seconds // 60) - grace_min)


# ── Enrollment ────────────────────────────────────────────────────────────────

@app.post("/workers/enroll")
async def enroll_worker(
    full_name: str = Form(...),
    shift: str = Form(...),
    telegram_chat_id: int | None = Form(None),
    photo: UploadFile = None,
):
    if shift not in ("morning", "night"):
        raise HTTPException(400, "shift must be 'morning' or 'night'")

    photo_bytes = await photo.read()
    try:
        embedding = face_match.get_embedding(photo_bytes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        del photo_bytes

    async with pool.acquire() as conn:
        async with conn.transaction():
            worker_id = await conn.fetchval(
                """
                INSERT INTO workers (full_name, telegram_chat_id, face_embedding, shift)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                full_name, telegram_chat_id, embedding, shift,
            )
            # Automatically create an open-ended schedule entry.
            shift_row = await conn.fetchrow(
                "SELECT id FROM shifts WHERE name = $1", shift
            )
            if shift_row:
                await conn.execute(
                    """
                    INSERT INTO schedules (worker_id, shift_id, effective_from)
                    VALUES ($1, $2, CURRENT_DATE)
                    """,
                    worker_id, shift_row["id"],
                )

    return {"worker_id": worker_id, "status": "enrolled"}


# ── Check-in / check-out ──────────────────────────────────────────────────────

@app.post("/attendance/check-in")
async def check_in(
    latitude: float = Form(...),
    longitude: float = Form(...),
    mock_location: bool = Form(False),
    photo: UploadFile = None,
):
    photo_bytes = await photo.read()

    async with pool.acquire() as conn:
        workers  = await conn.fetch(
            "SELECT id, face_embedding FROM workers WHERE active = TRUE"
        )
        enrolled = [(w["id"], w["face_embedding"]) for w in workers]

        liveness_ok = face_match.check_liveness(photo_bytes)
        worker_id, confidence, status = face_match.match_face(photo_bytes, enrolled)
        del photo_bytes

        if worker_id is None:
            raise HTTPException(422, "Face not recognized. Try again or contact your manager.")

        if not liveness_ok:
            status = "manual_review"

        office   = await get_office_location(conn)
        distance = haversine_meters(
            latitude, longitude, office["latitude"], office["longitude"]
        )
        if distance > office["radius_meters"]:
            status = "manual_review"
        if mock_location:
            status = "manual_review"

        now           = datetime.now().astimezone()
        shift_cfg     = await get_active_schedule(conn, worker_id, now.date())

        if not shift_cfg:
            raise HTTPException(
                422, "No shift schedule found for this worker. Contact your manager."
            )

        last_event = await conn.fetchrow(
            """
            SELECT * FROM attendance_events
            WHERE worker_id = $1 AND server_timestamp::date = CURRENT_DATE
            ORDER BY id DESC LIMIT 1
            """,
            worker_id,
        )
        event_type = (
            "check_out"
            if (last_event and last_event["event_type"] == "check_in")
            else "check_in"
        )

        minutes_late = None
        fee = 0.0
        if event_type == "check_in":
            minutes_late = compute_lateness(
                shift_cfg["start_time"], now, shift_cfg["grace_period_min"]
            )
            fee = round(minutes_late * float(shift_cfg["fee_per_min"]), 2)

        prev_hash = await get_last_hash(conn)
        payload   = {
            "worker_id":    worker_id,
            "event_type":   event_type,
            "confidence":   confidence,
            "status":       status,
            "liveness":     liveness_ok,
            "lat":          latitude,
            "lon":          longitude,
            "distance":     distance,
            "mock_location":mock_location,
            "minutes_late": minutes_late,
            "fee":          fee,
            "ts":           now.isoformat(),
        }
        row_hash = compute_row_hash(prev_hash, payload)

        worker   = await conn.fetchrow(
            "SELECT full_name, telegram_chat_id FROM workers WHERE id = $1", worker_id
        )
        event_id = await conn.fetchval(
            """
            INSERT INTO attendance_events (
                worker_id, event_type, server_timestamp, match_confidence,
                match_status, liveness_passed, latitude, longitude,
                distance_from_office_m, mock_location_flag, minutes_late,
                fee_charged, prev_hash, row_hash
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            RETURNING id
            """,
            worker_id, event_type, now, confidence, status, liveness_ok,
            latitude, longitude, distance, mock_location, minutes_late,
            fee, prev_hash, row_hash,
        )

    # Telegram notification (best-effort).
    if worker["telegram_chat_id"]:
        _send_checkin_notification(
            chat_id      = worker["telegram_chat_id"],
            name         = worker["full_name"],
            event_type   = event_type,
            minutes_late = minutes_late,
            fee          = fee,
        )

    return {
        "event_id":    event_id,
        "worker_name": worker["full_name"],
        "event_type":  event_type,
        "minutes_late":minutes_late,
        "fee_charged": fee,
        "status":      status,
    }


def _send_checkin_notification(
    chat_id: int, name: str, event_type: str, minutes_late: int | None, fee: float
) -> None:
    if event_type == "check_in":
        if minutes_late and minutes_late > 0:
            msg = (
                f"[Checkly] {name} checked in — {minutes_late} min late. "
                f"Fee: ${fee:.2f}."
            )
        else:
            msg = f"[Checkly] {name} checked in on time."
    else:
        msg = f"[Checkly] {name} checked out."
    telegram_bot.send_message(chat_id, msg)


# ── Attendance sheet ──────────────────────────────────────────────────────────

@app.get("/attendance/sheet")
async def attendance_sheet(day: date | None = None):
    day = day or date.today()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id, w.full_name, e.event_type, e.server_timestamp,
                   e.minutes_late, e.fee_charged, e.match_status, e.match_confidence
            FROM attendance_events e
            JOIN workers w ON w.id = e.worker_id
            WHERE e.server_timestamp::date = $1
            ORDER BY e.server_timestamp
            """,
            day,
        )
    return [dict(r) for r in rows]


@app.get("/attendance/daily-summary")
async def daily_summary_endpoint(day: date | None = None):
    day = day or date.today()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM daily_summary WHERE summary_date = $1 ORDER BY shift",
            day,
        )
    return [dict(r) for r in rows]


@app.get("/attendance/audit")
async def audit(from_date: date, to_date: date):
    """
    Per-worker summary for any date range — designed for biweekly payment processing.

    Returns for each worker:
      - days_checked_in, on_time_count, late_count, total_minutes_late
      - total_late_fees  (the fines to deduct from pay)
      - presence_checks_failed / excused / total triggered
      - days_absent      (working days with no check-in at all)

    Queries source tables directly so it works even if the nightly
    daily_summary job hasn't run yet for the period.

    Example: GET /attendance/audit?from_date=2026-07-01&to_date=2026-07-15
    """
    if to_date < from_date:
        raise HTTPException(400, "to_date must be on or after from_date")

    working_days = (to_date - from_date).days + 1

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                w.id        AS worker_id,
                w.full_name,
                w.shift,

                COALESCE(a.days_checked_in,    0) AS days_checked_in,
                COALESCE(a.on_time_count,      0) AS on_time_count,
                COALESCE(a.late_count,         0) AS late_count,
                COALESCE(a.total_minutes_late, 0) AS total_minutes_late,
                COALESCE(a.total_late_fees,    0) AS total_late_fees,

                COALESCE(p.checks_failed,      0) AS presence_checks_failed,
                COALESCE(p.checks_excused,     0) AS presence_checks_excused,
                COALESCE(p.checks_total,       0) AS presence_checks_total

            FROM workers w

            LEFT JOIN (
                SELECT
                    worker_id,
                    COUNT(DISTINCT server_timestamp::date)
                        FILTER (WHERE event_type = 'check_in')                    AS days_checked_in,
                    COUNT(*)
                        FILTER (WHERE event_type = 'check_in'
                                  AND (minutes_late IS NULL OR minutes_late = 0)
                                  AND match_status = 'accepted')                  AS on_time_count,
                    COUNT(*)
                        FILTER (WHERE event_type = 'check_in'
                                  AND minutes_late > 0)                           AS late_count,
                    COALESCE(SUM(minutes_late)
                        FILTER (WHERE event_type = 'check_in'), 0)                AS total_minutes_late,
                    COALESCE(SUM(fee_charged), 0)                                 AS total_late_fees
                FROM attendance_events
                WHERE server_timestamp::date BETWEEN $1 AND $2
                GROUP BY worker_id
            ) a ON a.worker_id = w.id

            LEFT JOIN (
                SELECT
                    worker_id,
                    COUNT(*) FILTER (WHERE result = 'failed')                     AS checks_failed,
                    COUNT(*) FILTER (WHERE result = 'excused')                    AS checks_excused,
                    COUNT(*) FILTER (WHERE result IN ('failed','passed','excused'))AS checks_total
                FROM presence_checks
                WHERE triggered_at::date BETWEEN $1 AND $2
                GROUP BY worker_id
            ) p ON p.worker_id = w.id

            WHERE w.active = TRUE
            ORDER BY w.shift, w.full_name
            """,
            from_date, to_date,
        )

    return [
        {
            **dict(r),
            "days_absent":    working_days - (r["days_checked_in"] or 0),
            "period_from":    from_date.isoformat(),
            "period_to":      to_date.isoformat(),
            "working_days":   working_days,
            "total_late_fees": float(r["total_late_fees"]),
        }
        for r in rows
    ]
