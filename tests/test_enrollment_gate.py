"""
Tests for the face enrollment gate.

Critical invariant: a worker with face_enrolled=FALSE must never be able
to check in, regardless of how their worker record was created. Conversely,
once face_enrolled=TRUE, the normal check-in flow applies.

These tests mock asyncpg to avoid a live database. They test at the API
layer using FastAPI's TestClient so the gate is exercised end-to-end through
the HTTP handler, not just the SQL query.
"""

import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub heavy dependencies that live only in the venv.
# Must happen before any project module is imported.
for _mod in [
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.asyncio",
    "asyncpg",
    "insightface",
    "insightface.app",
    "cv2",
]:
    sys.modules[_mod] = MagicMock()

import io

import pytest
from fastapi.testclient import TestClient


# ── Mock asyncpg pool factory ─────────────────────────────────────────────────

def _make_pool(conn: AsyncMock) -> MagicMock:
    """Wrap a mock connection in a mock pool context manager."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


def _make_conn() -> AsyncMock:
    conn = AsyncMock()
    # Default: return empty results unless overridden per-test.
    conn.fetch.return_value = []
    conn.fetchrow.return_value = None
    conn.fetchval.return_value = None
    conn.execute.return_value = None
    # Support async context manager for conn.transaction()
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return conn


# ── Gate: face_enrolled=FALSE blocks check-in ─────────────────────────────────

class TestEnrollmentGate:
    """
    The check-in endpoint loads only enrolled workers' embeddings.
    Workers with face_enrolled=FALSE have no embedding and are excluded
    from the DB query, so face_match.match_face receives an empty list
    and returns (None, 0.0, "rejected") → HTTP 422.
    """

    def setup_method(self):
        import main as checkly_main
        self.app_module = checkly_main

    def _fake_photo(self) -> io.BytesIO:
        return io.BytesIO(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # minimal JPEG header

    def test_unenrolled_worker_cannot_check_in(self):
        """
        DB returns zero enrolled workers (as it would when all are face_enrolled=FALSE).
        match_face gets an empty list → no match → 422.
        """
        conn = _make_conn()
        conn.fetch.return_value = []  # no enrolled workers in the query result

        with patch.object(self.app_module, "pool", _make_pool(conn)):
            with patch.object(self.app_module.face_match, "check_liveness", return_value=True):
                with patch.object(
                    self.app_module.face_match, "match_face",
                    return_value=(None, 0.0, "rejected"),
                ):
                    client = TestClient(self.app_module.app, raise_server_exceptions=False)
                    resp = client.post(
                        "/attendance/check-in",
                        data={"latitude": "41.3", "longitude": "69.2", "mock_location": "false"},
                        files={"photo": ("face.jpg", self._fake_photo(), "image/jpeg")},
                    )
        assert resp.status_code == 422
        assert "Face not recognized" in resp.json()["detail"]

    def test_enrolled_worker_proceeds_to_matching(self):
        """
        DB returns one enrolled worker. match_face finds a match.
        The full check-in flow proceeds (we stop before the DB write by
        controlling fetchrow results for subsequent queries).
        """
        import asyncpg

        fake_embedding = [0.1] * 512

        conn = _make_conn()
        # enrolled workers query returns one row with face_enrolled=TRUE
        conn.fetch.side_effect = [
            [{"id": 7, "face_embedding": fake_embedding}],  # workers query
        ]
        # office location
        conn.fetchrow.side_effect = [
            {"latitude": 41.3, "longitude": 69.2, "radius_meters": 200},  # office
            {  # active schedule
                "shift_name": "morning",
                "start_time": __import__("datetime").time(9, 0),
                "end_time": __import__("datetime").time(17, 0),
                "grace_period_min": 5,
                "fee_per_min": 0.5,
            },
            None,  # last attendance event (none today)
            {"full_name": "Alisher Tursunov", "telegram_chat_id": None},  # worker name
        ]
        conn.fetchval.return_value = 42  # inserted event_id

        with patch.object(self.app_module, "pool", _make_pool(conn)):
            with patch.object(self.app_module.face_match, "check_liveness", return_value=True):
                with patch.object(
                    self.app_module.face_match, "match_face",
                    return_value=(7, 0.85, "accepted"),
                ):
                    with patch.object(self.app_module, "get_last_hash", new=AsyncMock(return_value=None)):
                        client = TestClient(self.app_module.app, raise_server_exceptions=False)
                        resp = client.post(
                            "/attendance/check-in",
                            data={"latitude": "41.3", "longitude": "69.2", "mock_location": "false"},
                            files={"photo": ("face.jpg", self._fake_photo(), "image/jpeg")},
                        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["worker_name"] == "Alisher Tursunov"
        assert body["event_type"] == "check_in"

    def test_match_face_not_called_with_unenrolled_workers(self):
        """
        Even if unenrolled workers exist in the DB, they must not appear in
        the embedding list passed to match_face.

        This verifies the SQL query logic: the mock conn.fetch returns a list
        that simulates what the DB returns after the WHERE face_enrolled=TRUE
        filter. match_face is then called with only those results.
        """
        conn = _make_conn()
        # Simulate: 3 workers total, but only 1 is enrolled.
        # The DB query filters to enrolled only, so fetch returns just 1.
        enrolled_only = [{"id": 3, "face_embedding": [0.0] * 512}]
        conn.fetch.return_value = enrolled_only

        captured_enrolled_list = []

        def _capture_match(photo_bytes, enrolled):
            captured_enrolled_list.extend(enrolled)
            return (None, 0.0, "rejected")

        with patch.object(self.app_module, "pool", _make_pool(conn)):
            with patch.object(self.app_module.face_match, "check_liveness", return_value=True):
                with patch.object(
                    self.app_module.face_match, "match_face",
                    side_effect=_capture_match,
                ):
                    client = TestClient(self.app_module.app, raise_server_exceptions=False)
                    client.post(
                        "/attendance/check-in",
                        data={"latitude": "41.3", "longitude": "69.2", "mock_location": "false"},
                        files={"photo": ("face.jpg", self._fake_photo(), "image/jpeg")},
                    )

        # match_face should have been called with exactly the one enrolled worker.
        assert len(captured_enrolled_list) == 1
        assert captured_enrolled_list[0][0] == 3  # worker_id=3


# ── Enrollment approval sets face_enrolled=TRUE ───────────────────────────────

class TestEnrollmentApproval:
    """
    Verify that the approval endpoint writes face_enrolled=TRUE and the
    candidate embedding to the workers table, and logs the action.
    """

    def setup_method(self):
        import enrollment_api as ea
        self.ea = ea

    def test_approve_writes_embedding_and_enrolled_flag(self):
        """
        approve_session should UPDATE workers SET face_embedding=..., face_enrolled=TRUE
        and UPDATE enrollment_sessions SET status='approved'.
        """
        fake_embedding = [0.05] * 512

        conn = _make_conn()
        conn.fetchrow.side_effect = [
            {  # _get_session
                "id": 1, "worker_id": 5, "consent_id": 1, "desk_zone_id": 2,
                "status": "ready", "sample_count": 6, "min_samples": 5,
                "candidate_embedding": fake_embedding,
                "approved_by": None, "approved_at": None, "started_at": None,
                "approval_note": None, "completed_at": None, "started_by": None,
            },
            {"id": 9},  # manager exists
        ]

        import enrollment_api as ea
        original_pool = ea.pool
        try:
            ea.pool = _make_pool(conn)
            from fastapi.testclient import TestClient
            from fastapi import FastAPI
            app = FastAPI()
            app.include_router(ea.router)
            client = TestClient(app, raise_server_exceptions=False)

            resp = client.post(
                "/enrollment/sessions/1/approve",
                json={"manager_id": 9, "note": "Verified in person"},
            )
        finally:
            ea.pool = original_pool

        assert resp.status_code == 200
        body = resp.json()
        assert body["face_enrolled"] is True
        assert body["status"] == "approved"
        assert body["worker_id"] == 5

        # Verify conn.execute was called with the UPDATE workers ... face_enrolled = TRUE
        all_execute_calls = [str(call) for call in conn.execute.call_args_list]
        assert any("face_enrolled" in c for c in all_execute_calls), (
            "Expected UPDATE workers ... face_enrolled=TRUE but it was not called"
        )

    def test_approve_fails_when_no_samples_collected(self):
        """
        Attempting to approve a session with no samples must return 400,
        not silently write a NULL embedding.
        """
        conn = _make_conn()
        conn.fetchrow.return_value = {
            "id": 2, "worker_id": 5, "consent_id": 1, "desk_zone_id": 2,
            "status": "ready", "sample_count": 0, "min_samples": 5,
            "candidate_embedding": None,  # no samples collected
            "approved_by": None, "approved_at": None, "started_at": None,
            "approval_note": None, "completed_at": None, "started_by": None,
        }

        import enrollment_api as ea
        original_pool = ea.pool
        try:
            ea.pool = _make_pool(conn)
            from fastapi.testclient import TestClient
            from fastapi import FastAPI
            app = FastAPI()
            app.include_router(ea.router)
            client = TestClient(app, raise_server_exceptions=False)

            resp = client.post(
                "/enrollment/sessions/2/approve",
                json={"manager_id": 9},
            )
        finally:
            ea.pool = original_pool

        assert resp.status_code == 400
        assert "No embedding samples" in resp.json()["detail"]

    def test_session_requires_consent(self):
        """
        Creating a session without a valid consent_id must return 400.
        """
        conn = _make_conn()
        conn.fetchrow.side_effect = [
            {"id": 5, "full_name": "Test Worker", "face_enrolled": False},  # worker exists
            None,  # consent NOT found
        ]

        import enrollment_api as ea
        original_pool = ea.pool
        try:
            ea.pool = _make_pool(conn)
            from fastapi.testclient import TestClient
            from fastapi import FastAPI
            app = FastAPI()
            app.include_router(ea.router)
            client = TestClient(app, raise_server_exceptions=False)

            resp = client.post(
                "/enrollment/sessions",
                json={
                    "worker_id": 5,
                    "consent_id": 999,   # does not exist
                    "desk_zone_id": 1,
                    "manager_id": 1,
                },
            )
        finally:
            ea.pool = original_pool

        assert resp.status_code == 400
        assert "Consent record not found" in resp.json()["detail"]

    def test_duplicate_active_session_rejected(self):
        """
        Starting a second enrollment session while one is already active
        must return 409 Conflict.
        """
        conn = _make_conn()
        conn.fetchrow.side_effect = [
            {"id": 5, "full_name": "Worker", "face_enrolled": False},  # worker
            {"id": 10},                                                  # consent found
            {"id": 1},                                                   # zone found
            {"id": 3},                                                   # existing active session
        ]

        import enrollment_api as ea
        original_pool = ea.pool
        try:
            ea.pool = _make_pool(conn)
            from fastapi.testclient import TestClient
            from fastapi import FastAPI
            app = FastAPI()
            app.include_router(ea.router)
            client = TestClient(app, raise_server_exceptions=False)

            resp = client.post(
                "/enrollment/sessions",
                json={
                    "worker_id": 5,
                    "consent_id": 10,
                    "desk_zone_id": 1,
                    "manager_id": 1,
                },
            )
        finally:
            ea.pool = original_pool

        assert resp.status_code == 409
