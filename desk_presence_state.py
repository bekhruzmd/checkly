"""
Away/authorization state machine for desk-presence tracking.

Pure Python — no database or network I/O. Feed it events as they arrive
and it returns ResolvedLeave outcomes. The FastAPI router wraps this with
actual DB reads/writes (desk_presence_api.py).

Lifecycle per worker:

    desk_vacated ──────────────────────────────► [AwayRecord opened]
                                                          │
                  safe_zone_entered ────────────────────► │ (mark authorized)
                                                          │
    desk_returned ────────────────────────────────────────►
        gap < min_leave_seconds ─── NOISE    (detection jitter)
        authorized              ─── AUTHORIZED (visited safe zone)
        otherwise               ─── UNAUTHORIZED (fined, notified)

Edge cases handled (see tests/test_desk_presence_state.py):
  - Double vacancy (camera glitch) → reset start_ts, clear authorization
  - Safe zone after already returned → ignored (no open record)
  - Return without prior vacancy → returns None
  - Shift-end sweep → closes all open records as UNAUTHORIZED
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class LeaveOutcome(str, Enum):
    NOISE        = "noise"         # Gap under min_leave_seconds — detection jitter
    AUTHORIZED   = "authorized"    # Worker visited a safe zone before returning
    UNAUTHORIZED = "unauthorized"  # No safe-zone visit — counts toward threshold


@dataclass
class AwayRecord:
    worker_id: int
    origin_zone_id: int
    start_ts: datetime
    min_leave_seconds: int
    authorized: bool = False
    safe_zone_id: Optional[int] = None


@dataclass
class ResolvedLeave:
    worker_id: int
    origin_zone_id: int
    start_ts: datetime
    end_ts: datetime
    duration_seconds: float
    outcome: LeaveOutcome
    safe_zone_id: Optional[int] = None


class AwayStateMachine:
    """
    Tracks in-memory away state for all workers.

    Designed for single-threaded asyncio use in the FastAPI router — all
    transitions are plain dict operations with no awaits between reads and
    writes, so asyncio's cooperative scheduling keeps this consistent.
    """

    def __init__(self) -> None:
        self._away: dict[int, AwayRecord] = {}

    # ── Transitions ───────────────────────────────────────────────────────────

    def desk_vacated(
        self,
        worker_id: int,
        zone_id: int,
        ts: datetime,
        min_leave_seconds: int,
    ) -> None:
        """Worker's desk zone transitioned from occupied to empty."""
        if worker_id in self._away:
            # Camera glitch or worker moved between desks without being tracked
            # returning. Reset the clock and clear any prior authorization.
            rec = self._away[worker_id]
            rec.start_ts = ts
            rec.origin_zone_id = zone_id
            rec.min_leave_seconds = min_leave_seconds
            rec.authorized = False
            rec.safe_zone_id = None
        else:
            self._away[worker_id] = AwayRecord(
                worker_id=worker_id,
                origin_zone_id=zone_id,
                start_ts=ts,
                min_leave_seconds=min_leave_seconds,
            )

    def safe_zone_entered(
        self,
        worker_id: int,
        safe_zone_id: int,
        ts: datetime,  # noqa: ARG002 — kept for caller symmetry
    ) -> None:
        """Worker identified in a safe zone while tracked as away."""
        record = self._away.get(worker_id)
        if record is None:
            return  # Not away — ignore
        record.authorized = True
        record.safe_zone_id = safe_zone_id

    def desk_returned(
        self,
        worker_id: int,
        zone_id: int,  # noqa: ARG002 — kept for caller symmetry / future cross-desk logic
        ts: datetime,
    ) -> Optional[ResolvedLeave]:
        """
        Worker's desk transitioned from empty to occupied.

        Returns a ResolvedLeave if there was an open AwayRecord, else None.
        """
        record = self._away.pop(worker_id, None)
        if record is None:
            return None

        duration = (ts - record.start_ts).total_seconds()

        if duration < record.min_leave_seconds:
            outcome = LeaveOutcome.NOISE
        elif record.authorized:
            outcome = LeaveOutcome.AUTHORIZED
        else:
            outcome = LeaveOutcome.UNAUTHORIZED

        return ResolvedLeave(
            worker_id=worker_id,
            origin_zone_id=record.origin_zone_id,
            start_ts=record.start_ts,
            end_ts=ts,
            duration_seconds=duration,
            outcome=outcome,
            safe_zone_id=record.safe_zone_id,
        )

    def close_all_for_shift_end(
        self,
        worker_ids: list[int],
        ts: datetime,
    ) -> list[ResolvedLeave]:
        """
        Forcibly close open away records at shift end.

        Any gap that's still under min_leave_seconds is treated as noise
        (worker went briefly idle at end-of-shift). Longer absences are
        UNAUTHORIZED regardless of safe-zone visits — the worker never
        returned to their desk before the shift ended.
        """
        results: list[ResolvedLeave] = []
        for worker_id in worker_ids:
            record = self._away.pop(worker_id, None)
            if record is None:
                continue
            duration = (ts - record.start_ts).total_seconds()
            if duration < record.min_leave_seconds:
                continue  # too short — treat as noise
            results.append(
                ResolvedLeave(
                    worker_id=worker_id,
                    origin_zone_id=record.origin_zone_id,
                    start_ts=record.start_ts,
                    end_ts=ts,
                    duration_seconds=duration,
                    outcome=LeaveOutcome.UNAUTHORIZED,
                    safe_zone_id=record.safe_zone_id,
                )
            )
        return results

    # ── Startup reconstruction helpers ────────────────────────────────────────

    def restore_record(self, record: AwayRecord) -> None:
        """Restore an AwayRecord from DB state (called on backend startup)."""
        self._away[record.worker_id] = record

    def open_records(self) -> dict[int, AwayRecord]:
        """Snapshot of all open away records — used for debugging/monitoring."""
        return dict(self._away)
