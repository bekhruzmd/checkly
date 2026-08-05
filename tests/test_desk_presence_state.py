"""
Unit tests for the desk-presence away/authorization state machine.

Run with:  pytest tests/test_desk_presence_state.py -v

Tests cover every transition documented in desk_presence_state.py plus the
edge cases called out in the feature plan: double vacancy, safe-zone visit
after return, unidentified-worker events, and shift-end sweep.
"""

import sys
import os

# Allow importing from the project root without installing as a package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta

import pytest

from desk_presence_state import AwayRecord, AwayStateMachine, LeaveOutcome


BASE_TS = datetime(2026, 8, 5, 9, 0, 0)
MIN_LEAVE = 60  # seconds


def ts(offset_seconds: float) -> datetime:
    return BASE_TS + timedelta(seconds=offset_seconds)


# ── Basic transitions ─────────────────────────────────────────────────────────

class TestUnauthorizedLeave:
    def test_gap_above_threshold_no_safe_zone(self):
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        result = sm.desk_returned(1, zone_id=1, ts=ts(120))
        assert result is not None
        assert result.outcome == LeaveOutcome.UNAUTHORIZED
        assert result.duration_seconds == pytest.approx(120)
        assert result.safe_zone_id is None

    def test_duration_recorded_correctly(self):
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        result = sm.desk_returned(1, zone_id=1, ts=ts(90))
        assert result.duration_seconds == pytest.approx(90)

    def test_worker_id_and_origin_zone_preserved(self):
        sm = AwayStateMachine()
        sm.desk_vacated(42, zone_id=7, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        result = sm.desk_returned(42, zone_id=7, ts=ts(200))
        assert result.worker_id == 42
        assert result.origin_zone_id == 7


class TestAuthorizedLeave:
    def test_safe_zone_visit_authorizes_leave(self):
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.safe_zone_entered(1, safe_zone_id=99, ts=ts(30))
        result = sm.desk_returned(1, zone_id=1, ts=ts(120))
        assert result.outcome == LeaveOutcome.AUTHORIZED
        assert result.safe_zone_id == 99

    def test_multiple_safe_zone_visits_last_wins(self):
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.safe_zone_entered(1, safe_zone_id=10, ts=ts(20))
        sm.safe_zone_entered(1, safe_zone_id=11, ts=ts(40))
        result = sm.desk_returned(1, zone_id=1, ts=ts(120))
        assert result.outcome == LeaveOutcome.AUTHORIZED
        assert result.safe_zone_id == 11  # most recent wins


class TestNoise:
    def test_gap_under_threshold_is_noise(self):
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        result = sm.desk_returned(1, zone_id=1, ts=ts(30))
        assert result.outcome == LeaveOutcome.NOISE

    def test_exactly_at_threshold_is_not_noise(self):
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        result = sm.desk_returned(1, zone_id=1, ts=ts(MIN_LEAVE))
        assert result.outcome != LeaveOutcome.NOISE

    def test_noise_with_safe_zone_still_noise(self):
        """A safe zone visit during a sub-threshold gap is still noise."""
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.safe_zone_entered(1, safe_zone_id=2, ts=ts(10))
        result = sm.desk_returned(1, zone_id=1, ts=ts(30))
        assert result.outcome == LeaveOutcome.NOISE


# ── No-op / guard cases ───────────────────────────────────────────────────────

class TestNoopCases:
    def test_return_without_prior_vacancy_returns_none(self):
        sm = AwayStateMachine()
        result = sm.desk_returned(1, zone_id=1, ts=ts(0))
        assert result is None

    def test_safe_zone_without_prior_vacancy_is_ignored(self):
        """Worker seen in safe zone when not tracked as away → no effect."""
        sm = AwayStateMachine()
        sm.safe_zone_entered(1, safe_zone_id=2, ts=ts(0))
        # No away record should have been created.
        result = sm.desk_returned(1, zone_id=1, ts=ts(200))
        assert result is None

    def test_safe_zone_after_return_is_ignored(self):
        """Safe zone event after the worker has already returned → no open record."""
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.desk_returned(1, zone_id=1, ts=ts(90))  # worker returned
        sm.safe_zone_entered(1, safe_zone_id=2, ts=ts(100))  # spurious safe-zone event
        result = sm.desk_returned(1, zone_id=1, ts=ts(300))
        assert result is None  # no open record to close

    def test_occupied_end_without_worker_id_does_not_create_record(self):
        """
        When the face match fails on occupied_end, worker_id is None.
        The state machine should not be called without a worker_id — this
        test documents the expected caller behaviour (API validates first).
        """
        sm = AwayStateMachine()
        # The router only calls desk_vacated when worker_id is known.
        # So there should be no record if vacated was never called.
        result = sm.desk_returned(99, zone_id=1, ts=ts(120))
        assert result is None


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_double_vacancy_resets_start_time(self):
        """
        Camera glitch sends two occupied_end events for the same worker.
        The second event resets the start timestamp, so the gap is measured
        from the second vacancy, not the first.
        """
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.desk_vacated(1, zone_id=1, ts=ts(50), min_leave_seconds=MIN_LEAVE)
        result = sm.desk_returned(1, zone_id=1, ts=ts(100))
        # Gap from ts=50 to ts=100 is 50 s → noise
        assert result.outcome == LeaveOutcome.NOISE
        assert result.duration_seconds == pytest.approx(50)

    def test_double_vacancy_clears_prior_authorization(self):
        """Second vacancy resets authorization from any prior safe-zone visit."""
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.safe_zone_entered(1, safe_zone_id=2, ts=ts(10))
        # Camera glitch: another vacancy event before they return
        sm.desk_vacated(1, zone_id=1, ts=ts(20), min_leave_seconds=MIN_LEAVE)
        result = sm.desk_returned(1, zone_id=1, ts=ts(120))
        # Safe zone visit was before the re-vacating — authorization cleared
        assert result.outcome == LeaveOutcome.UNAUTHORIZED
        assert result.safe_zone_id is None

    def test_two_workers_are_fully_independent(self):
        """Two workers' away records don't influence each other."""
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.desk_vacated(2, zone_id=2, ts=ts(10), min_leave_seconds=MIN_LEAVE)
        sm.safe_zone_entered(2, safe_zone_id=3, ts=ts(40))  # only worker 2

        r1 = sm.desk_returned(1, zone_id=1, ts=ts(120))
        r2 = sm.desk_returned(2, zone_id=2, ts=ts(120))

        assert r1.outcome == LeaveOutcome.UNAUTHORIZED
        assert r2.outcome == LeaveOutcome.AUTHORIZED

    def test_worker_vacates_different_zone_on_return(self):
        """Worker leaves desk zone 1 but is detected at desk zone 2 on return.
        The away record is still resolved (cross-desk return is valid)."""
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        result = sm.desk_returned(1, zone_id=2, ts=ts(120))
        assert result is not None
        assert result.outcome == LeaveOutcome.UNAUTHORIZED
        assert result.origin_zone_id == 1  # where they left from

    def test_per_zone_min_leave_seconds_respected(self):
        """Each zone can have its own min_leave_seconds threshold."""
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=30)
        result = sm.desk_returned(1, zone_id=1, ts=ts(35))
        # 35s > 30s threshold → not noise
        assert result.outcome == LeaveOutcome.UNAUTHORIZED

    def test_away_record_consumed_on_return(self):
        """After a return resolves the record, subsequent returns return None."""
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.desk_returned(1, zone_id=1, ts=ts(120))
        result = sm.desk_returned(1, zone_id=1, ts=ts(240))
        assert result is None


# ── Shift-end sweep ───────────────────────────────────────────────────────────

class TestShiftEndSweep:
    def test_open_records_closed_as_unauthorized(self):
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.desk_vacated(2, zone_id=2, ts=ts(10), min_leave_seconds=MIN_LEAVE)
        results = sm.close_all_for_shift_end([1, 2], ts=ts(3600))
        assert len(results) == 2
        by_worker = {r.worker_id: r for r in results}
        assert by_worker[1].outcome == LeaveOutcome.UNAUTHORIZED
        assert by_worker[2].outcome == LeaveOutcome.UNAUTHORIZED

    def test_safe_zone_at_shift_end_still_unauthorized(self):
        """
        Worker visited safe zone but never returned to desk.
        At shift end this counts as unauthorized — they didn't complete
        the return leg, so the leave is unresolved.
        """
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.safe_zone_entered(1, safe_zone_id=2, ts=ts(30))
        results = sm.close_all_for_shift_end([1], ts=ts(3600))
        assert results[0].outcome == LeaveOutcome.UNAUTHORIZED

    def test_noise_duration_not_counted_at_shift_end(self):
        """A sub-threshold absence at shift end is treated as noise — not counted."""
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        results = sm.close_all_for_shift_end([1], ts=ts(30))  # only 30 s gap
        assert len(results) == 0  # noise — excluded

    def test_workers_not_away_are_skipped(self):
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        # Worker 2 has no record
        results = sm.close_all_for_shift_end([1, 2], ts=ts(3600))
        assert len(results) == 1
        assert results[0].worker_id == 1

    def test_records_removed_from_machine_after_sweep(self):
        """close_all_for_shift_end should clear the records it processes."""
        sm = AwayStateMachine()
        sm.desk_vacated(1, zone_id=1, ts=ts(0), min_leave_seconds=MIN_LEAVE)
        sm.close_all_for_shift_end([1], ts=ts(3600))
        assert sm.open_records() == {}


# ── Startup reconstruction ────────────────────────────────────────────────────

class TestStartupReconstruction:
    def test_restore_record_makes_worker_away(self):
        sm = AwayStateMachine()
        record = AwayRecord(
            worker_id=5,
            origin_zone_id=3,
            start_ts=ts(0),
            min_leave_seconds=60,
        )
        sm.restore_record(record)
        result = sm.desk_returned(5, zone_id=3, ts=ts(120))
        assert result is not None
        assert result.outcome == LeaveOutcome.UNAUTHORIZED

    def test_restored_record_appears_in_open_records(self):
        sm = AwayStateMachine()
        record = AwayRecord(
            worker_id=7,
            origin_zone_id=2,
            start_ts=ts(0),
            min_leave_seconds=60,
        )
        sm.restore_record(record)
        open_recs = sm.open_records()
        assert 7 in open_recs
        assert open_recs[7].origin_zone_id == 2
