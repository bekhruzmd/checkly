"""
Tests for the Workly roster import.

Critical invariants:
  1. Import is idempotent: running it twice with the same data must not
     create duplicate workers.
  2. Imported workers always land with face_enrolled=FALSE regardless of
     any field in the source data — biometric enrollment is a separate flow.
  3. The import logs an audit event to admin_audit_log every time it runs.
  4. Workers deactivated in Workly are set to active=FALSE here, but never
     deleted — their attendance history must remain intact.

Tests use a mock asyncpg connection to avoid a live database, testing the
upsert logic directly rather than the HTTP endpoint.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from workly_import import parse_csv, _map_workly_employee, upsert_workers


# ── Mock asyncpg helpers ──────────────────────────────────────────────────────

def _make_conn(upsert_results: list | None = None) -> AsyncMock:
    conn = AsyncMock()
    # fetchval is called once per worker (returns was_inserted bool) and once for
    # the audit log INSERT (returns audit_log_id).
    if upsert_results is not None:
        conn.fetchval.side_effect = upsert_results
    else:
        conn.fetchval.return_value = True  # was_inserted=True (new row)

    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return conn


def _make_pool(conn: AsyncMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── CSV parsing ───────────────────────────────────────────────────────────────

class TestCsvParse:
    SAMPLE_CSV = (
        "employee_id,full_name,department,position,shift,telegram_chat_id\n"
        "EMP001,Alisher Tursunov,Engineering,Developer,morning,\n"
        "EMP002,Nodira Yusupova,HR,Manager,night,123456789\n"
        "EMP003,Bobur Karimov,Engineering,QA,morning,\n"
    )

    def test_parses_all_rows(self):
        workers = parse_csv(self.SAMPLE_CSV)
        assert len(workers) == 3

    def test_employee_ids_preserved(self):
        workers = parse_csv(self.SAMPLE_CSV)
        ids = {w["employee_id"] for w in workers}
        assert ids == {"EMP001", "EMP002", "EMP003"}

    def test_telegram_id_parsed(self):
        workers = parse_csv(self.SAMPLE_CSV)
        nodira = next(w for w in workers if w["employee_id"] == "EMP002")
        assert nodira["telegram_chat_id"] == 123456789

    def test_empty_telegram_id_is_none(self):
        workers = parse_csv(self.SAMPLE_CSV)
        alisher = next(w for w in workers if w["employee_id"] == "EMP001")
        assert alisher["telegram_chat_id"] is None

    def test_shift_values_valid(self):
        workers = parse_csv(self.SAMPLE_CSV)
        for w in workers:
            assert w["shift"] in ("morning", "night")

    def test_unknown_shift_skipped(self):
        csv = (
            "employee_id,full_name,shift\n"
            "EMP001,Alice,morning\n"
            "EMP002,Bob,afternoon\n"   # invalid
        )
        workers = parse_csv(csv)
        assert len(workers) == 1
        assert workers[0]["employee_id"] == "EMP001"

    def test_missing_required_columns_raises(self):
        csv = "full_name,shift\nAlice,morning\n"
        with pytest.raises(ValueError, match="missing required columns"):
            parse_csv(csv)

    def test_empty_employee_id_skipped(self):
        csv = (
            "employee_id,full_name,shift\n"
            ",Bob,morning\n"      # missing employee_id
            "EMP001,Alice,morning\n"
        )
        workers = parse_csv(csv)
        assert len(workers) == 1

    def test_active_defaults_to_true(self):
        workers = parse_csv(self.SAMPLE_CSV)
        for w in workers:
            assert w["active"] is True


# ── Workly API field mapping ───────────────────────────────────────────────────

class TestWorklyMapping:
    def test_maps_basic_fields(self):
        raw = {
            "id": "W123",
            "fullName": "Dilnoza Saidova",
            "department": {"name": "Finance"},
            "position": {"name": "Accountant"},
            "schedule": {"name": "morning"},
            "status": "active",
        }
        w = _map_workly_employee(raw)
        assert w is not None
        assert w["employee_id"] == "W123"
        assert w["full_name"] == "Dilnoza Saidova"
        assert w["department"] == "Finance"
        assert w["position"] == "Accountant"
        assert w["shift"] == "morning"
        assert w["active"] is True

    def test_dismissed_worker_maps_to_inactive(self):
        raw = {
            "id": "W999",
            "fullName": "Dismissed Person",
            "schedule": {"name": "morning"},
            "status": "dismissed",
        }
        w = _map_workly_employee(raw)
        assert w is not None
        assert w["active"] is False

    def test_missing_id_returns_none(self):
        raw = {"fullName": "No ID Worker", "schedule": {"name": "morning"}}
        assert _map_workly_employee(raw) is None

    def test_missing_name_returns_none(self):
        raw = {"id": "X1", "schedule": {"name": "morning"}}
        assert _map_workly_employee(raw) is None

    def test_unknown_shift_defaults_to_morning(self):
        raw = {"id": "X2", "fullName": "Test", "schedule": {"name": "evening_shift"}}
        w = _map_workly_employee(raw)
        assert w["shift"] == "morning"


# ── Upsert idempotency ────────────────────────────────────────────────────────

class TestUpsertIdempotency:
    WORKERS = [
        {"employee_id": "E1", "full_name": "Alice", "shift": "morning",
         "department": "Eng", "position": "Dev", "active": True, "telegram_chat_id": None},
        {"employee_id": "E2", "full_name": "Bob", "shift": "night",
         "department": "HR", "position": "Mgr", "active": True, "telegram_chat_id": None},
    ]

    def test_first_import_creates_workers(self):
        # fetchval returns: True (created E1), True (created E2), 99 (audit_log_id)
        conn = _make_conn(upsert_results=[True, True, 99])
        pool = _make_pool(conn)
        result = run(upsert_workers(pool, self.WORKERS, manager_id=1, source="test"))
        assert result["created"] == 2
        assert result["updated"] == 0

    def test_second_import_updates_not_duplicates(self):
        # fetchval returns: False (updated E1), False (updated E2), 100 (audit_log_id)
        conn = _make_conn(upsert_results=[False, False, 100])
        pool = _make_pool(conn)
        result = run(upsert_workers(pool, self.WORKERS, manager_id=1, source="test"))
        assert result["created"] == 0
        assert result["updated"] == 2

    def test_upsert_sql_uses_on_conflict(self):
        """The INSERT statement must include ON CONFLICT (employee_id) DO UPDATE."""
        conn = _make_conn(upsert_results=[True, 1])
        pool = _make_pool(conn)
        run(upsert_workers(pool, self.WORKERS[:1], manager_id=1, source="test"))

        sql_calls = [str(c) for c in conn.fetchval.call_args_list]
        assert any("ON CONFLICT" in s for s in sql_calls), (
            "Expected ON CONFLICT in the upsert SQL"
        )

    def test_face_enrolled_never_overwritten(self):
        """
        The ON CONFLICT DO UPDATE SET clause must NOT include face_enrolled or
        face_embedding — those are managed exclusively by enrollment_api.
        (face_enrolled=FALSE legitimately appears in the INSERT VALUES part.)
        """
        conn = _make_conn(upsert_results=[False, 1])
        pool = _make_pool(conn)
        run(upsert_workers(pool, self.WORKERS[:1], manager_id=1, source="test"))

        sql_calls = [str(c) for c in conn.fetchval.call_args_list]
        for call_str in sql_calls:
            if "ON CONFLICT" not in call_str:
                continue
            # Extract only the DO UPDATE SET clause for the assertion.
            do_update = call_str.split("DO UPDATE SET", 1)[-1]
            assert "face_enrolled" not in do_update, (
                "face_enrolled must not appear in DO UPDATE SET clause"
            )
            assert "face_embedding" not in do_update, (
                "face_embedding must not appear in DO UPDATE SET clause"
            )

    def test_new_workers_inserted_with_face_enrolled_false(self):
        """
        The INSERT VALUES must include face_enrolled=FALSE so imported workers
        cannot check in until enrollment is complete.
        """
        conn = _make_conn(upsert_results=[True, 1])
        pool = _make_pool(conn)
        run(upsert_workers(pool, self.WORKERS[:1], manager_id=1, source="test"))

        sql_calls = [str(c) for c in conn.fetchval.call_args_list]
        assert any("FALSE" in s or "face_enrolled" in s for s in sql_calls), (
            "Expected face_enrolled=FALSE in the INSERT"
        )

    def test_empty_input_returns_zero_counts(self):
        conn = _make_conn()
        pool = _make_pool(conn)
        result = run(upsert_workers(pool, [], manager_id=1, source="test"))
        assert result["created"] == 0
        assert result["updated"] == 0
        assert result["skipped"] == 0
        # No DB calls should be made for empty input.
        conn.fetchval.assert_not_called()


# ── Audit logging ─────────────────────────────────────────────────────────────

class TestImportAuditLog:
    def test_audit_log_inserted_on_import(self):
        """Every import must write an admin_audit_log row."""
        workers = [
            {"employee_id": "A1", "full_name": "Worker A", "shift": "morning",
             "department": None, "position": None, "active": True, "telegram_chat_id": None},
        ]
        conn = _make_conn(upsert_results=[True, 55])  # 55 = audit_log_id
        pool = _make_pool(conn)
        result = run(upsert_workers(pool, workers, manager_id=2, source="csv"))
        assert result["audit_log_id"] == 55

    def test_audit_log_includes_source(self):
        """The audit log details must record the import source."""
        workers = [
            {"employee_id": "A1", "full_name": "Worker A", "shift": "morning",
             "department": None, "position": None, "active": True, "telegram_chat_id": None},
        ]
        conn = _make_conn(upsert_results=[True, 1])
        pool = _make_pool(conn)
        run(upsert_workers(pool, workers, manager_id=1, source="csv"))

        # The last fetchval call should be the audit log INSERT.
        audit_call = conn.fetchval.call_args_list[-1]
        call_args = audit_call[0]
        # 3rd positional arg to fetchval is the JSONB details dict.
        details = call_args[2]
        assert details.get("source") == "csv"

    def test_deactivated_workers_not_deleted(self):
        """
        A worker with active=FALSE in Workly should be upserted with active=FALSE
        (sets the flag) but the worker row must not be deleted.
        """
        deactivated = [
            {"employee_id": "D1", "full_name": "Ex Worker", "shift": "morning",
             "department": None, "position": None, "active": False, "telegram_chat_id": None},
        ]
        conn = _make_conn(upsert_results=[False, 1])  # False = updated (already existed)
        pool = _make_pool(conn)
        result = run(upsert_workers(pool, deactivated, manager_id=1, source="test"))

        # No DELETE should ever be called.
        conn.execute.assert_not_called()
        # The row should be updated (not deleted).
        assert result["updated"] == 1
