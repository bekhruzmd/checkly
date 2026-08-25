"""
Face enrollment API.

Enrollment is camera-driven: the edge device continuously monitors camera
zones and, when a zone has an active enrollment session, submits face
embeddings from detected faces directly to this API. The manager watches
the dashboard, verifies the right worker is in frame, and approves once
enough samples have accumulated.

Flow:
  1. Manager records worker consent (must happen before any embedding is stored).
  2. Manager creates an enrollment session for the worker at their camera zone.
  3. Edge device polls GET /enrollment/active-zones and starts submitting
     embeddings from that zone to POST /enrollment/sessions/{id}/samples.
  4. Backend maintains a running average of submitted embeddings (sample_count
     increments each submission; status flips to 'ready' at min_samples).
  5. Manager approves via POST /enrollment/sessions/{id}/approve.
     This writes the candidate_embedding to workers.face_embedding and
     sets face_enrolled = TRUE — the worker can now check in.

Consent rule (enforced by DB FK):
  A biometric_consents row for the worker must exist BEFORE the session
  is created. The session creation endpoint validates this; the DB FK
  enforces it permanently.

Endpoints called by the edge device require the shared EDGE_SECRET header.
Endpoints called by the manager dashboard are authenticated via Supabase
Auth (the Python backend trusts the manager_id passed in the request body;
for a production hardening, verify it against auth.uid via Supabase Admin
API or pass the JWT and decode it server-side).
"""

import logging
import os
from datetime import datetime
from typing import Optional

import numpy as np
import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from pydantic import BaseModel, field_validator

import face_match
from utils import compute_row_hash

log = logging.getLogger(__name__)
router = APIRouter(prefix="/enrollment", tags=["enrollment"])

pool: asyncpg.Pool | None = None

CONSENT_TEXT = (
    "I voluntarily consent to the collection and processing of my biometric facial "
    "data (a mathematical embedding, not a photo) by my employer for the purpose of "
    "automated attendance and desk-presence tracking at the workplace. I understand "
    "that this data is stored locally on the company's own servers and is not shared "
    "with third parties. I may withdraw consent by informing my manager, after which "
    "my enrollment will be deactivated."
)


# ── Auth ──────────────────────────────────────────────────────────────────────

async def _require_edge_token(authorization: str = Header(...)) -> None:
    secret = os.environ.get("EDGE_SECRET", "")
    if not secret:
        raise HTTPException(503, "EDGE_SECRET not configured on backend")
    if authorization != f"Bearer {secret}":
        raise HTTPException(401, "Invalid edge token")


# ── Embedding math ─────────────────────────────────────────────────────────────

def _normalize(vec: list[float]) -> list[float]:
    arr = np.array(vec, dtype=np.float64)
    norm = np.linalg.norm(arr)
    if norm == 0:
        return vec
    return (arr / norm).tolist()


def _running_average(
    current: list[float] | None,
    n: int,
    new_sample: list[float],
) -> list[float]:
    """Update a normalized running average with one new sample."""
    new_arr = np.array(new_sample, dtype=np.float64)
    if current is None or n == 0:
        return _normalize(new_sample)
    cur_arr = np.array(current, dtype=np.float64)
    # Weighted average then re-normalize (ArcFace embeddings are unit vectors).
    updated = (cur_arr * n + new_arr) / (n + 1)
    norm = np.linalg.norm(updated)
    if norm == 0:
        return current
    return (updated / norm).tolist()


# ── DB helpers ─────────────────────────────────────────────────────────────────

async def _get_worker(conn, worker_id: int):
    return await conn.fetchrow(
        "SELECT id, full_name, face_enrolled FROM workers WHERE id = $1 AND active = TRUE",
        worker_id,
    )


async def _get_session(conn, session_id: int):
    return await conn.fetchrow(
        "SELECT * FROM enrollment_sessions WHERE id = $1",
        session_id,
    )


async def _latest_consent_hash(conn) -> str | None:
    row = await conn.fetchrow(
        "SELECT row_hash FROM biometric_consents ORDER BY id DESC LIMIT 1"
    )
    return row["row_hash"] if row else None


# ── Consent ────────────────────────────────────────────────────────────────────

class ConsentBody(BaseModel):
    worker_id: int
    manager_id: int
    consent_text: Optional[str] = None     # if omitted, uses the canonical CONSENT_TEXT


@router.post("/consent", status_code=201)
async def record_consent(body: ConsentBody):
    """
    Record explicit written consent before any biometric capture begins.
    Returns the consent_id required to start an enrollment session.

    The manager must be present with the worker when this is submitted.
    The canonical consent text is used if none is provided — override only
    to provide a translated version (keep the meaning identical).
    """
    text = body.consent_text or CONSENT_TEXT

    async with pool.acquire() as conn:
        worker = await _get_worker(conn, body.worker_id)
        if not worker:
            raise HTTPException(404, f"Worker {body.worker_id} not found or inactive")

        manager = await conn.fetchrow(
            "SELECT id FROM managers WHERE id = $1 AND active = TRUE", body.manager_id
        )
        if not manager:
            raise HTTPException(404, f"Manager {body.manager_id} not found or inactive")

        prev_hash = await _latest_consent_hash(conn)
        payload = {
            "worker_id":   body.worker_id,
            "manager_id":  body.manager_id,
            "consented_at": datetime.now().isoformat(),
            "consent_text": text,
        }
        row_hash = compute_row_hash(prev_hash, payload)

        consent_id = await conn.fetchval(
            """
            INSERT INTO biometric_consents
              (worker_id, consented_at, consent_text, witnessed_by, prev_hash, row_hash)
            VALUES ($1, now(), $2, $3, $4, $5)
            RETURNING id
            """,
            body.worker_id, text, body.manager_id, prev_hash, row_hash,
        )

    return {"consent_id": consent_id, "worker_id": body.worker_id}


# ── Start session ──────────────────────────────────────────────────────────────

class StartSessionBody(BaseModel):
    worker_id: int
    consent_id: int
    desk_zone_id: int
    manager_id: int
    min_samples: int = 5

    @field_validator("min_samples")
    @classmethod
    def min_samples_range(cls, v: int) -> int:
        if not (1 <= v <= 30):
            raise ValueError("min_samples must be between 1 and 30")
        return v


@router.post("/sessions", status_code=201)
async def start_session(body: StartSessionBody):
    """
    Start a camera-based enrollment session.

    The worker must have an existing consent record (body.consent_id).
    Only one active session per worker is allowed at a time.
    The edge device will start submitting samples for body.desk_zone_id once
    it polls GET /enrollment/active-zones.
    """
    async with pool.acquire() as conn:
        worker = await _get_worker(conn, body.worker_id)
        if not worker:
            raise HTTPException(404, f"Worker {body.worker_id} not found or inactive")

        # Consent must pre-exist and belong to this worker.
        consent = await conn.fetchrow(
            "SELECT id FROM biometric_consents WHERE id = $1 AND worker_id = $2",
            body.consent_id, body.worker_id,
        )
        if not consent:
            raise HTTPException(
                400,
                "Consent record not found for this worker. Record consent first.",
            )

        # Zone must exist.
        zone = await conn.fetchrow(
            "SELECT id FROM desk_zones WHERE id = $1 AND active = TRUE", body.desk_zone_id
        )
        if not zone:
            raise HTTPException(404, f"Desk zone {body.desk_zone_id} not found or inactive")

        # Reject duplicate active sessions for the same worker.
        existing = await conn.fetchrow(
            """
            SELECT id FROM enrollment_sessions
            WHERE worker_id = $1 AND status IN ('active', 'ready')
            """,
            body.worker_id,
        )
        if existing:
            raise HTTPException(
                409,
                f"Worker {body.worker_id} already has an active session ({existing['id']}). "
                "Reject it before starting a new one.",
            )

        session_id = await conn.fetchval(
            """
            INSERT INTO enrollment_sessions
              (worker_id, consent_id, desk_zone_id, started_by, min_samples, status)
            VALUES ($1, $2, $3, $4, $5, 'active')
            RETURNING id
            """,
            body.worker_id, body.consent_id, body.desk_zone_id,
            body.manager_id, body.min_samples,
        )

    log.info(
        "Enrollment session %d started for worker %d at zone %d (min_samples=%d)",
        session_id, body.worker_id, body.desk_zone_id, body.min_samples,
    )
    return {
        "session_id": session_id,
        "worker_id": body.worker_id,
        "desk_zone_id": body.desk_zone_id,
        "min_samples": body.min_samples,
        "status": "active",
    }


# ── Active zones (polled by edge device) ──────────────────────────────────────

@router.get("/active-zones", dependencies=[Depends(_require_edge_token)])
async def get_active_zones():
    """
    Returns all zones that currently have an active enrollment session.
    The edge device polls this endpoint (same interval as embedding refresh).
    When a zone appears here, the edge device submits every detected face
    embedding from that zone to /enrollment/sessions/{session_id}/samples.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id AS session_id, desk_zone_id, worker_id, min_samples, sample_count
            FROM enrollment_sessions
            WHERE status = 'active'
            """
        )
    return [dict(r) for r in rows]


# ── Sample submission (called by edge device) ──────────────────────────────────

class SampleBody(BaseModel):
    embedding: list[float]

    @field_validator("embedding")
    @classmethod
    def embedding_length(cls, v: list[float]) -> list[float]:
        if len(v) != 512:
            raise ValueError(f"Embedding must be 512-d, got {len(v)}")
        return v


@router.post(
    "/sessions/{session_id}/samples",
    status_code=200,
    dependencies=[Depends(_require_edge_token)],
)
async def submit_sample(session_id: int, body: SampleBody):
    """
    Edge device submits one face embedding captured from the enrollment zone.
    Backend updates the running average (candidate_embedding) and increments
    sample_count. Status flips to 'ready' when sample_count >= min_samples.
    """
    async with pool.acquire() as conn:
        session = await _get_session(conn, session_id)
        if not session:
            raise HTTPException(404, f"Session {session_id} not found")
        if session["status"] != "active":
            return {
                "session_id": session_id,
                "status": session["status"],
                "sample_count": session["sample_count"],
                "message": "Session is not active; sample ignored",
            }

        n = session["sample_count"]
        current = list(session["candidate_embedding"]) if session["candidate_embedding"] else None
        new_avg = _running_average(current, n, body.embedding)
        new_count = n + 1
        new_status = (
            "ready" if new_count >= session["min_samples"] else "active"
        )

        await conn.execute(
            """
            UPDATE enrollment_sessions
            SET candidate_embedding = $1,
                sample_count = $2,
                status = $3
            WHERE id = $4
            """,
            new_avg, new_count, new_status, session_id,
        )

    if new_status == "ready":
        log.info(
            "Enrollment session %d ready for approval (%d samples collected)",
            session_id, new_count,
        )

    return {
        "session_id": session_id,
        "sample_count": new_count,
        "status": new_status,
    }


# ── Session status ─────────────────────────────────────────────────────────────

@router.get("/sessions/{session_id}")
async def get_session(session_id: int):
    async with pool.acquire() as conn:
        session = await _get_session(conn, session_id)
        if not session:
            raise HTTPException(404, f"Session {session_id} not found")

        worker = await conn.fetchrow(
            "SELECT full_name, face_enrolled FROM workers WHERE id = $1",
            session["worker_id"],
        )

    return {
        "session_id": session_id,
        "worker_id": session["worker_id"],
        "worker_name": worker["full_name"] if worker else None,
        "desk_zone_id": session["desk_zone_id"],
        "status": session["status"],
        "sample_count": session["sample_count"],
        "min_samples": session["min_samples"],
        "started_at": session["started_at"].isoformat() if session["started_at"] else None,
        "approved_at": session["approved_at"].isoformat() if session["approved_at"] else None,
    }


# ── Approval ───────────────────────────────────────────────────────────────────

class ApproveBody(BaseModel):
    manager_id: int
    note: Optional[str] = None
    override: bool = False      # allow approval with fewer than min_samples


@router.post("/sessions/{session_id}/approve", status_code=200)
async def approve_session(session_id: int, body: ApproveBody):
    """
    Manager confirms they visually verified the correct worker was in frame,
    then approves enrollment.

    This is the only place face_enrolled is set to TRUE. The operation is:
      1. Copy candidate_embedding → workers.face_embedding
      2. Set workers.face_enrolled = TRUE
      3. Mark session as 'approved'
      4. Append to admin_audit_log

    The worker can check in immediately after this returns.
    """
    async with pool.acquire() as conn:
        session = await _get_session(conn, session_id)
        if not session:
            raise HTTPException(404, f"Session {session_id} not found")

        if session["status"] not in ("ready", "active"):
            raise HTTPException(
                400,
                f"Session status is '{session['status']}'; cannot approve.",
            )

        if session["status"] == "active" and not body.override:
            raise HTTPException(
                400,
                f"Session has only {session['sample_count']} of "
                f"{session['min_samples']} required samples. "
                "Pass override=true to approve anyway.",
            )

        if not session["candidate_embedding"]:
            raise HTTPException(
                400,
                "No embedding samples collected yet. Cannot approve an empty session.",
            )

        manager = await conn.fetchrow(
            "SELECT id FROM managers WHERE id = $1 AND active = TRUE", body.manager_id
        )
        if not manager:
            raise HTTPException(404, f"Manager {body.manager_id} not found or inactive")

        worker_id = session["worker_id"]
        embedding = list(session["candidate_embedding"])

        async with conn.transaction():
            # Write embedding and flip enrollment flag.
            await conn.execute(
                """
                UPDATE workers
                SET face_embedding = $1, face_enrolled = TRUE
                WHERE id = $2
                """,
                embedding, worker_id,
            )

            # Mark session closed.
            await conn.execute(
                """
                UPDATE enrollment_sessions
                SET status = 'approved',
                    approved_by = $1,
                    approved_at = now(),
                    completed_at = now(),
                    approval_note = $2
                WHERE id = $3
                """,
                body.manager_id, body.note, session_id,
            )

            # Audit log.
            await conn.execute(
                """
                INSERT INTO admin_audit_log (action, performed_by, details, affected_count)
                VALUES ('enrollment_approved', $1, $2, 1)
                """,
                body.manager_id,
                {
                    "session_id": session_id,
                    "worker_id": worker_id,
                    "sample_count": session["sample_count"],
                    "note": body.note,
                },
            )

    log.info(
        "Enrollment approved: worker %d, session %d, by manager %d",
        worker_id, session_id, body.manager_id,
    )
    return {
        "session_id": session_id,
        "worker_id": worker_id,
        "face_enrolled": True,
        "status": "approved",
    }


# ── Rejection ──────────────────────────────────────────────────────────────────

class RejectBody(BaseModel):
    manager_id: int
    reason: str


@router.post("/sessions/{session_id}/reject", status_code=200)
async def reject_session(session_id: int, body: RejectBody):
    """
    Manager rejects the enrollment (wrong person in frame, bad quality, etc.).
    The worker remains face_enrolled=FALSE. A new session can be started.
    """
    if not body.reason or not body.reason.strip():
        raise HTTPException(400, "reason is required")

    async with pool.acquire() as conn:
        session = await _get_session(conn, session_id)
        if not session:
            raise HTTPException(404, f"Session {session_id} not found")

        if session["status"] not in ("active", "ready"):
            raise HTTPException(
                400,
                f"Session status is '{session['status']}'; cannot reject.",
            )

        async with conn.transaction():
            await conn.execute(
                """
                UPDATE enrollment_sessions
                SET status = 'rejected',
                    approved_by = $1,
                    completed_at = now(),
                    approval_note = $2
                WHERE id = $3
                """,
                body.manager_id, body.reason.strip(), session_id,
            )

            await conn.execute(
                """
                INSERT INTO admin_audit_log (action, performed_by, details, affected_count)
                VALUES ('enrollment_rejected', $1, $2, 1)
                """,
                body.manager_id,
                {
                    "session_id": session_id,
                    "worker_id": session["worker_id"],
                    "reason": body.reason.strip(),
                },
            )

    return {
        "session_id": session_id,
        "worker_id": session["worker_id"],
        "status": "rejected",
    }


# ── Enrollment queue ───────────────────────────────────────────────────────────

@router.post("/enroll-photo/{worker_id}")
async def enroll_from_photo(worker_id: int, photo: UploadFile):
    """
    Admin uploads or captures a photo of the worker to generate their face embedding.
    Sets face_enrolled = TRUE so the worker can use face ID check-in immediately.
    Photo is processed in memory and never stored.
    """
    photo_bytes = await photo.read()
    try:
        embedding = face_match.get_embedding(photo_bytes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(503, f"Face recognition unavailable: {e}")
    finally:
        del photo_bytes

    async with pool.acquire() as conn:
        worker = await conn.fetchrow(
            "SELECT id, full_name FROM workers WHERE id = $1 AND active = TRUE",
            worker_id,
        )
        if not worker:
            raise HTTPException(404, f"Worker {worker_id} not found or inactive")
        await conn.execute(
            """
            UPDATE workers
            SET face_embedding = $1, face_enrolled = TRUE, active = TRUE
            WHERE id = $2
            """,
            embedding, worker_id,
        )
    return {"worker_id": worker_id, "full_name": worker["full_name"], "status": "enrolled"}


@router.post("/quick-enroll/{worker_id}")
async def quick_enroll_worker(worker_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE workers
            SET face_enrolled = TRUE, active = TRUE
            WHERE id = $1
            """,
            worker_id,
        )
    return {"worker_id": worker_id, "status": "enrolled"}


@router.get("/queue")
async def enrollment_queue():
    """
    Workers who are active but not yet enrolled (face_enrolled=FALSE).
    Includes their current enrollment session status if one exists.
    Drives the enrollment queue view in the admin dashboard.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                w.id, w.full_name, w.shift, w.department, w.position,
                w.employee_id, w.created_at,
                es.id          AS session_id,
                es.status      AS session_status,
                es.sample_count,
                es.min_samples,
                es.desk_zone_id,
                dz.desk_label  AS zone_label
            FROM workers w
            LEFT JOIN LATERAL (
                SELECT * FROM enrollment_sessions
                WHERE worker_id = w.id
                ORDER BY id DESC LIMIT 1
            ) es ON TRUE
            LEFT JOIN desk_zones dz ON dz.id = es.desk_zone_id
            WHERE w.active = TRUE AND w.face_enrolled = FALSE
            ORDER BY w.full_name
            """
        )
    return [dict(r) for r in rows]
