"""
Nightly daily_summary aggregation job.

Scheduled to run at 05:30 each morning (after the night shift ends,
before the morning shift starts) via APScheduler in main.py.

Computes one row per shift for the given date and upserts into daily_summary.
Run manually for backfills: python aggregation.py 2026-07-30
"""

import asyncio
import logging
import sys
from datetime import date, timedelta

import asyncpg

DATABASE_URL = "postgresql://user:password@localhost:5432/attendance"
log = logging.getLogger(__name__)


async def compute_daily_summary(pool: asyncpg.Pool, for_date: date) -> None:
    async with pool.acquire() as conn:
        for shift_name in ("morning", "night"):
            shift = await conn.fetchrow(
                "SELECT * FROM shifts WHERE name = $1", shift_name
            )
            if not shift:
                continue

            # Workers assigned to this shift on for_date via the schedules table.
            workers_on_shift = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT sc.worker_id)
                FROM schedules sc
                WHERE sc.shift_id = $1
                  AND sc.effective_from <= $2
                  AND (sc.effective_to IS NULL OR sc.effective_to >= $2)
                """,
                shift["id"], for_date,
            )

            # Attendance stats from attendance_events.
            # Night shift (21:00–05:00) spans two calendar dates; we use the
            # server_timestamp date of the check-in event as the anchor.
            agg = await conn.fetchrow(
                """
                SELECT
                    COUNT(DISTINCT e.worker_id) FILTER (WHERE e.event_type = 'check_in')   AS checked_in,
                    COUNT(*)                    FILTER (WHERE e.event_type = 'check_in'
                                                          AND (e.minutes_late IS NULL OR e.minutes_late = 0)
                                                          AND e.match_status = 'accepted')   AS on_time,
                    COUNT(*)                    FILTER (WHERE e.event_type = 'check_in'
                                                          AND e.minutes_late > 0)             AS late,
                    COALESCE(SUM(e.minutes_late) FILTER (WHERE e.event_type = 'check_in'), 0) AS total_minutes_late,
                    COALESCE(SUM(e.fee_charged),  0)                                          AS total_fees
                FROM attendance_events e
                JOIN workers w ON w.id = e.worker_id
                WHERE e.server_timestamp::date = $1
                  AND w.shift = $2
                """,
                for_date, shift_name,
            )

            # Presence check stats.
            pc = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)                                              AS total,
                    COUNT(*) FILTER (WHERE result = 'passed')            AS passed,
                    COUNT(*) FILTER (WHERE result = 'failed')            AS failed,
                    COUNT(*) FILTER (WHERE result = 'excused')           AS excused
                FROM presence_checks pc
                JOIN workers w ON w.id = pc.worker_id
                WHERE pc.triggered_at::date = $1
                  AND w.shift = $2
                """,
                for_date, shift_name,
            )

            await conn.execute(
                """
                INSERT INTO daily_summary (
                    summary_date, shift,
                    total_workers, checked_in, on_time, late,
                    total_minutes_late, total_fees,
                    presence_checks_total, presence_checks_passed,
                    presence_checks_failed, presence_checks_excused,
                    computed_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12, now())
                ON CONFLICT (summary_date, shift) DO UPDATE SET
                    total_workers          = EXCLUDED.total_workers,
                    checked_in             = EXCLUDED.checked_in,
                    on_time                = EXCLUDED.on_time,
                    late                   = EXCLUDED.late,
                    total_minutes_late     = EXCLUDED.total_minutes_late,
                    total_fees             = EXCLUDED.total_fees,
                    presence_checks_total  = EXCLUDED.presence_checks_total,
                    presence_checks_passed = EXCLUDED.presence_checks_passed,
                    presence_checks_failed = EXCLUDED.presence_checks_failed,
                    presence_checks_excused= EXCLUDED.presence_checks_excused,
                    computed_at            = now()
                """,
                for_date,
                shift_name,
                workers_on_shift or 0,
                agg["checked_in"]        or 0,
                agg["on_time"]           or 0,
                agg["late"]              or 0,
                agg["total_minutes_late"] or 0,
                agg["total_fees"]        or 0,
                pc["total"]              or 0,
                pc["passed"]             or 0,
                pc["failed"]             or 0,
                pc["excused"]            or 0,
            )
            log.info("daily_summary upserted: %s / %s", for_date, shift_name)


async def run_for_yesterday(pool: asyncpg.Pool) -> None:
    yesterday = date.today() - timedelta(days=1)
    await compute_daily_summary(pool, yesterday)


# ── CLI entry point for manual backfills ──────────────────────────────────────

async def _main():
    target = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today() - timedelta(days=1)
    pool = await asyncpg.create_pool(DATABASE_URL)
    try:
        await compute_daily_summary(pool, target)
        print(f"Done: {target}")
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
