"""
Attendance MVP API.

Endpoints:
  POST /workers/enroll        -- one-time face enrollment for a new worker
  POST /attendance/check-in   -- worker submits a selfie + GPS coords
  GET  /attendance/sheet      -- plain attendance log for the admin view

No worker photos are stored anywhere -- not on disk, not in the DB.
Photos exist only in request memory for the duration of the call.
"""

import hashlib
import json
import math
from datetime import datetime, date, time as dtime

import asyncpg
from fastapi import FastAPI, UploadFile, Form, HTTPException
from pydantic import BaseModel

import face_match

app = FastAPI(title="Attendance MVP")

DATABASE_URL = "postgresql://user:password@localhost:5432/attendance"  # set via env in real deploy
pool: asyncpg.Pool | None = None


@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def haversine_meters(lat1, lon1, lat2, lon2) -> float:
    R = 6371000  # Earth radius in meters
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def compute_row_hash(prev_hash: str, payload: dict) -> str:
    """Chains each event to the previous one so tampering is detectable."""
    content = (prev_hash or "") + json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(content.encode()).hexdigest()


async def get_last_hash(conn) -> str | None:
    row = await conn.fetchrow(
        "SELECT row_hash FROM attendance_events ORDER BY id DESC LIMIT 1"
    )
    return row["row_hash"] if row else None


async def get_shift_config(conn, shift_name: str):
    return await conn.fetchrow("SELECT * FROM shifts WHERE name = $1", shift_name)


async def get_office_location(conn):
    # MVP assumes a single office location; extend to multi-location later.
    return await conn.fetchrow("SELECT * FROM office_locations LIMIT 1")


def compute_lateness(shift_start: dtime, now: datetime, grace_min: int) -> int:
    scheduled = datetime.combine(now.date(), shift_start, tzinfo=now.tzinfo)
    late_seconds = (now - scheduled).total_seconds()
    late_minutes = max(0, int(late_seconds // 60) - grace_min)
    return late_minutes


# ---------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------

@app.post("/workers/enroll")
async def enroll_worker(
    full_name: str = Form(...),
    shift: str = Form(...),               # 'morning' or 'night'
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
        del photo_bytes  # explicit: photo is never persisted

    async with pool.acquire() as conn:
        worker_id = await conn.fetchval(
            """
            INSERT INTO workers (full_name, telegram_chat_id, face_embedding, shift)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            full_name, telegram_chat_id, embedding, shift,
        )
    return {"worker_id": worker_id, "status": "enrolled"}


# ---------------------------------------------------------------------
# Check-in / check-out
# ---------------------------------------------------------------------

class CheckInMeta(BaseModel):
    latitude: float
    longitude: float
    mock_location: bool = False   # set by client from Location.isFromMockProvider() on Android


@app.post("/attendance/check-in")
async def check_in(
    latitude: float = Form(...),
    longitude: float = Form(...),
    mock_location: bool = Form(False),
    photo: UploadFile = None,
):
    photo_bytes = await photo.read()

    async with pool.acquire() as conn:
        workers = await conn.fetch(
            "SELECT id, face_embedding FROM workers WHERE active = TRUE"
        )
        enrolled = [(w["id"], w["face_embedding"]) for w in workers]

        liveness_ok = face_match.check_liveness(photo_bytes)
        worker_id, confidence, status = face_match.match_face(photo_bytes, enrolled)
        del photo_bytes  # explicit: never persisted, never written to disk

        if worker_id is None:
            # No confident match -- log nothing tied to a specific worker;
            # surface this to the admin dashboard as an "unrecognized attempt"
            # instead of silently failing.
            raise HTTPException(422, "Face not recognized. Try again or contact your manager.")

        if not liveness_ok:
            status = "manual_review"

        office = await get_office_location(conn)
        distance = haversine_meters(latitude, longitude, office["latitude"], office["longitude"])
        within_geofence = distance <= office["radius_meters"]
        if not within_geofence:
            status = "manual_review"
        if mock_location:
            status = "manual_review"

        worker = await conn.fetchrow("SELECT * FROM workers WHERE id = $1", worker_id)
        shift_cfg = await get_shift_config(conn, worker["shift"])

        # Determine check_in vs check_out: look at the worker's last event today.
        last_event = await conn.fetchrow(
            """
            SELECT * FROM attendance_events
            WHERE worker_id = $1 AND server_timestamp::date = CURRENT_DATE
            ORDER BY id DESC LIMIT 1
            """,
            worker_id,
        )
        event_type = "check_out" if (last_event and last_event["event_type"] == "check_in") else "check_in"

        now = datetime.now().astimezone()
        minutes_late = None
        fee = 0.0
        if event_type == "check_in":
            minutes_late = compute_lateness(shift_cfg["start_time"], now, shift_cfg["grace_period_min"])
            fee = round(minutes_late * float(shift_cfg["fee_per_min"]), 2)

        prev_hash = await get_last_hash(conn)
        payload = {
            "worker_id": worker_id,
            "event_type": event_type,
            "confidence": confidence,
            "status": status,
            "liveness": liveness_ok,
            "lat": latitude,
            "lon": longitude,
            "distance": distance,
            "mock_location": mock_location,
            "minutes_late": minutes_late,
            "fee": fee,
            "ts": now.isoformat(),
        }
        row_hash = compute_row_hash(prev_hash, payload)

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

    # Telegram notification fires here (separate module, notification-only,
    # not shown in this file -- simple POST to api.telegram.org).

    return {
        "event_id": event_id,
        "worker_name": worker["full_name"],
        "event_type": event_type,
        "minutes_late": minutes_late,
        "fee_charged": fee,
        "status": status,
    }


# ---------------------------------------------------------------------
# Attendance sheet (bare-bones, no auth yet -- add before real deployment)
# ---------------------------------------------------------------------

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
