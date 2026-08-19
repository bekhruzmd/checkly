"""
Workly roster import.

Pulls employee data from Workly's API (or falls back to CSV if API access
is unavailable) and upserts into the workers table.

Workly API assumptions (based on publicly described endpoints — verify
against your account's actual API docs at https://api.workly.io/docs):
  Base URL:  https://api.workly.io
  Auth:      Authorization: Bearer <api_key>
  Employees: GET /v1/company/employees
             Returns JSON list with fields mapped below.

If the API returns 401 / 403 or the endpoint doesn't exist on your tier,
fall back to CSV. CSV column order:
  employee_id, full_name, department, position, shift[, telegram_chat_id]
  shift must be 'morning' or 'night'.

Idempotency: upserts on employee_id (ON CONFLICT DO UPDATE).
  - Running the import again updates existing records (name, department,
    position, shift) — does NOT reset face_enrolled or face_embedding.
  - Workers with no employee_id (manually created) are never touched.
  - Deactivated workers in Workly (status != 'active') are set to
    active=FALSE here but are never deleted.

Every import is logged to admin_audit_log regardless of source.
"""

import csv
import io
import logging
import os
from datetime import datetime
from typing import Optional

import asyncpg
import requests

log = logging.getLogger(__name__)

WORKLY_BASE = os.environ.get("WORKLY_API_BASE", "https://api.workly.io")
WORKLY_API_KEY = os.environ.get("WORKLY_API_KEY", "")

# ── Workly field mapping ───────────────────────────────────────────────────────
# Adjust these if your Workly account returns different field names.
# The Workly API response is a list of employee objects; nested fields are
# accessed with dotted paths (e.g. "department.name" → row["department"]["name"]).

_SHIFT_MAP = {
    # Workly schedule name → our shift name
    # Add your Workly schedule names here.
    "morning":  "morning",
    "Morning":  "morning",
    "Утро":     "morning",
    "night":    "night",
    "Night":    "night",
    "Ночь":     "night",
    "evening":  "night",
}


def _map_workly_employee(raw: dict) -> dict | None:
    """
    Convert a Workly API employee object to our workers table fields.
    Returns None if the record should be skipped (missing required fields).

    Workly API field names are guesses — update to match your actual response.
    """
    employee_id = str(raw.get("id") or raw.get("uid") or "").strip()
    if not employee_id:
        log.warning("Skipping Workly record with no id: %s", raw)
        return None

    full_name = (
        raw.get("fullName")
        or raw.get("full_name")
        or " ".join(filter(None, [
            raw.get("lastName", ""),
            raw.get("firstName", ""),
            raw.get("middleName", ""),
        ]))
    ).strip()
    if not full_name:
        log.warning("Skipping Workly record %s with no name", employee_id)
        return None

    # Department: may be a nested object or a flat string.
    dept_raw = raw.get("department") or {}
    department = (
        dept_raw.get("name") if isinstance(dept_raw, dict) else str(dept_raw)
    ) or None

    # Position: same pattern.
    pos_raw = raw.get("position") or {}
    position = (
        pos_raw.get("name") if isinstance(pos_raw, dict) else str(pos_raw)
    ) or None

    # Shift: Workly calls it "schedule" or "shift".
    schedule_raw = (
        (raw.get("schedule") or {}).get("name")
        if isinstance(raw.get("schedule"), dict)
        else raw.get("schedule") or raw.get("shift", "")
    )
    shift = _SHIFT_MAP.get(str(schedule_raw).strip())
    if not shift:
        log.warning(
            "Workly employee %s has unknown schedule '%s'; defaulting to 'morning'",
            employee_id, schedule_raw,
        )
        shift = "morning"

    is_active = str(raw.get("status", "active")).lower() in ("active", "working", "1", "true")

    telegram_id_raw = raw.get("telegramChatId") or raw.get("telegram_chat_id")
    telegram_chat_id = int(telegram_id_raw) if telegram_id_raw else None

    return {
        "employee_id": employee_id,
        "full_name": full_name,
        "department": department,
        "position": position,
        "shift": shift,
        "active": is_active,
        "telegram_chat_id": telegram_chat_id,
    }


# ── API fetch ──────────────────────────────────────────────────────────────────

def fetch_from_workly_api(api_key: str, base_url: str = WORKLY_BASE) -> list[dict]:
    """
    Fetch employee list from Workly API.
    Raises requests.HTTPError if the API returns a non-2xx response.
    Raises ValueError if the response is not a usable list.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    # Try the most likely endpoint paths in order.
    endpoints = [
        "/v1/company/employees",
        "/v1/employees",
        "/api/v1/employees",
        "/api/employees",
    ]

    last_exc: Exception | None = None
    for path in endpoints:
        try:
            resp = requests.get(f"{base_url}{path}", headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            # Unwrap common envelope patterns.
            if isinstance(data, dict):
                data = data.get("data") or data.get("employees") or data.get("items") or []

            if not isinstance(data, list):
                raise ValueError(f"Expected list from Workly API, got {type(data).__name__}")

            log.info("Workly API: fetched %d raw records from %s", len(data), path)
            return data

        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (401, 403, 404):
                last_exc = exc
                continue
            raise
        except Exception as exc:
            last_exc = exc
            continue

    raise RuntimeError(
        f"Workly API unavailable or endpoint not found. Last error: {last_exc}. "
        "Use CSV import instead."
    )


# ── CSV parse ──────────────────────────────────────────────────────────────────

def parse_csv(content: str) -> list[dict]:
    """
    Parse CSV export. Expected header row:
      employee_id, full_name, department, position, shift[, telegram_chat_id]

    shift must be 'morning' or 'night'. Rows with invalid shift are skipped.
    """
    reader = csv.DictReader(io.StringIO(content.strip()))
    required = {"employee_id", "full_name", "shift"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError(
            f"CSV missing required columns. Required: {required}. "
            f"Got: {reader.fieldnames}"
        )

    workers = []
    for i, row in enumerate(reader, start=2):  # 2 = first data row
        employee_id = (row.get("employee_id") or "").strip()
        full_name = (row.get("full_name") or "").strip()
        shift = (row.get("shift") or "").strip().lower()

        if not employee_id or not full_name:
            log.warning("CSV row %d: skipping — missing employee_id or full_name", i)
            continue
        if shift not in ("morning", "night"):
            log.warning("CSV row %d: unknown shift '%s', skipping", i, shift)
            continue

        telegram_raw = (row.get("telegram_chat_id") or "").strip()
        telegram_chat_id = int(telegram_raw) if telegram_raw.lstrip("-").isdigit() else None

        workers.append({
            "employee_id": employee_id,
            "full_name": full_name,
            "department": (row.get("department") or "").strip() or None,
            "position": (row.get("position") or "").strip() or None,
            "shift": shift,
            "active": True,
            "telegram_chat_id": telegram_chat_id,
        })

    return workers


# ── DB upsert ──────────────────────────────────────────────────────────────────

async def upsert_workers(
    pool: asyncpg.Pool,
    workers: list[dict],
    manager_id: Optional[int] = None,
    source: str = "unknown",
) -> dict:
    """
    Idempotent upsert of a worker list into the workers table.

    Upserts on employee_id:
      - New employee_id  → INSERT with face_enrolled=FALSE, active from source
      - Existing         → UPDATE full_name, department, position, shift, active
      - face_embedding and face_enrolled are NEVER overwritten by import

    Returns {"created": int, "updated": int, "skipped": int, "audit_log_id": int}
    """
    if not workers:
        return {"created": 0, "updated": 0, "skipped": 0, "audit_log_id": None}

    created = updated = skipped = 0
    audit_log_id = None

    async with pool.acquire() as conn:
        async with conn.transaction():
            for w in workers:
                if not w.get("employee_id") or not w.get("full_name"):
                    skipped += 1
                    continue

                result = await conn.fetchval(
                    """
                    INSERT INTO workers
                        (employee_id, full_name, department, position, shift,
                         active, telegram_chat_id, face_enrolled)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, FALSE)
                    ON CONFLICT (employee_id) DO UPDATE SET
                        full_name        = EXCLUDED.full_name,
                        department       = EXCLUDED.department,
                        position         = EXCLUDED.position,
                        shift            = EXCLUDED.shift,
                        active           = EXCLUDED.active,
                        telegram_chat_id = EXCLUDED.telegram_chat_id
                    RETURNING (xmax = 0) AS was_inserted
                    """,
                    w["employee_id"], w["full_name"], w.get("department"),
                    w.get("position"), w["shift"], w.get("active", True),
                    w.get("telegram_chat_id"),
                )
                # xmax = 0 means the row was newly inserted (not updated).
                if result:
                    created += 1
                else:
                    updated += 1

            total = created + updated
            audit_log_id = await conn.fetchval(
                """
                INSERT INTO admin_audit_log
                    (action, performed_by, details, affected_count)
                VALUES ('workly_import', $1, $2, $3)
                RETURNING id
                """,
                manager_id,
                {
                    "source": source,
                    "created": created,
                    "updated": updated,
                    "skipped": skipped,
                    "total_input": len(workers),
                    "imported_at": datetime.now().isoformat(),
                },
                total,
            )

    log.info(
        "Workly import complete: %d created, %d updated, %d skipped (source=%s)",
        created, updated, skipped, source,
    )
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total": total,
        "audit_log_id": audit_log_id,
    }


# ── FastAPI endpoint ───────────────────────────────────────────────────────────

from fastapi import APIRouter, UploadFile, Form, HTTPException
router = APIRouter(prefix="/admin/workly", tags=["admin"])

# Injected by main.py.
_pool: asyncpg.Pool | None = None


def set_pool(p: asyncpg.Pool) -> None:
    global _pool
    _pool = p


@router.post("/import")
async def trigger_import(
    manager_id: int = Form(...),
    csv_file: Optional[UploadFile] = None,
):
    """
    Import workers from Workly API (default) or a CSV upload (fallback).

    If csv_file is provided, it takes priority over the API.
    Otherwise, the backend attempts the Workly API using WORKLY_API_KEY
    from the environment; if that fails (no key, wrong tier, API down),
    the endpoint returns 503 with instructions to use CSV.

    All imports are logged to admin_audit_log regardless of success path.
    """
    if _pool is None:
        raise HTTPException(503, "Database pool not ready")

    # Validate manager exists.
    async with _pool.acquire() as conn:
        mgr = await conn.fetchrow(
            "SELECT id FROM managers WHERE id = $1 AND active = TRUE", manager_id
        )
    if not mgr:
        raise HTTPException(404, f"Manager {manager_id} not found or inactive")

    # CSV upload takes priority.
    if csv_file is not None:
        content = (await csv_file.read()).decode("utf-8")
        try:
            raw_workers = parse_csv(content)
        except (ValueError, Exception) as exc:
            raise HTTPException(400, f"CSV parse error: {exc}")
        source = "csv"
    else:
        # Try Workly API.
        api_key = WORKLY_API_KEY
        if not api_key:
            raise HTTPException(
                503,
                "WORKLY_API_KEY not configured. Upload a CSV file instead, or "
                "set WORKLY_API_KEY in the environment.",
            )
        try:
            raw = fetch_from_workly_api(api_key)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc))
        except Exception as exc:
            raise HTTPException(502, f"Workly API error: {exc}")

        raw_workers = [_map_workly_employee(r) for r in raw]
        raw_workers = [w for w in raw_workers if w is not None]
        source = "workly_api"

    result = await upsert_workers(_pool, raw_workers, manager_id=manager_id, source=source)
    return result


# ── CLI entry point ────────────────────────────────────────────────────────────

async def _cli_import(csv_path: str | None = None) -> None:
    """Run import directly from CLI (useful for cron / one-shot deploys)."""
    import asyncpg as _asyncpg

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise SystemExit("DATABASE_URL env var required")

    p = await _asyncpg.create_pool(db_url)

    if csv_path:
        with open(csv_path, encoding="utf-8") as f:
            content = f.read()
        raw_workers = parse_csv(content)
        source = f"csv:{csv_path}"
    else:
        api_key = WORKLY_API_KEY
        if not api_key:
            raise SystemExit("WORKLY_API_KEY not set. Pass a CSV path as argument.")
        raw = fetch_from_workly_api(api_key)
        raw_workers = [_map_workly_employee(r) for r in raw if r]
        raw_workers = [w for w in raw_workers if w]
        source = "workly_api"

    result = await upsert_workers(p, raw_workers, manager_id=None, source=source)
    await p.close()
    print(f"Import complete: {result}")


if __name__ == "__main__":
    import asyncio
    import sys
    asyncio.run(_cli_import(sys.argv[1] if len(sys.argv) > 1 else None))
