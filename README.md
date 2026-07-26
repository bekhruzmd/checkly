# Attendance MVP

Face-check-in attendance system. No worker photos are stored anywhere —
only a one-time face embedding (enrollment) and per-event metadata
(timestamp, GPS, confidence score). Every check-in photo is processed
in memory and discarded.

## Structure

- `schema.sql` — Postgres schema. Run this first to create the database.
- `face_match.py` — InsightFace wrapper: enrollment embedding + check-in matching + basic liveness check.
- `main.py` — FastAPI backend: enroll, check-in, attendance sheet.
- `requirements.txt` — Python dependencies.
- `webapp/` — Standalone PWA: camera + GPS capture, posts to the API. Installable to home screen.

## Running locally

```bash
# 1. Create the database
createdb attendance
psql attendance < schema.sql

# 2. Install dependencies
pip install -r requirements.txt --break-system-packages

# 3. Set your real DB connection string in main.py (DATABASE_URL)

# 4. Run the API
uvicorn main:app --reload --port 8000

# 5. Serve the webapp (any static file server) and set API_BASE
#    in webapp/index.html to your API's URL
python -m http.server 8080 --directory webapp
```

## What's deliberately NOT in this MVP (add before real deployment)

- **Auth on `/attendance/sheet`** — right now anyone with the URL can view it.
  Add an admin login before this touches real data.
- **Telegram notifications** — the hook point is marked in `main.py`
  (`check_in`, right after the DB insert). A simple POST to
  `api.telegram.org/bot<token>/sendMessage` using the worker's
  `telegram_chat_id` is all that's needed.
- **Reliable mock-location detection** — Android's
  `Location.isFromMockProvider()` isn't accessible from a browser PWA.
  If GPS spoofing turns out to be a real problem in practice, that's
  the strongest signal to revisit going native (or a WebView wrapper)
  for the check-in flow specifically.
- **Proper anti-spoofing** — the current liveness check is a blur/sharpness
  heuristic, good enough to catch a phone-photo-of-a-photo but not a
  determined attacker. InsightFace has a dedicated anti-spoof model
  (`antispoof` in some model packs) worth swapping in once you see real
  usage patterns.
- **Manual review UI** — events land with `match_status = 'manual_review'`
  when confidence is borderline, liveness fails, or the worker is outside
  the geofence. There's no admin screen to resolve these yet — for now,
  query `attendance_events WHERE match_status = 'manual_review'` directly.

## Audit trail design

Every row in `attendance_events` is hash-chained to the previous row
(`prev_hash` → `row_hash`). If anyone edits a row directly in the
database later, the hash chain breaks from that point forward and is
detectable by recomputing hashes over the table. Application code should
never `UPDATE` or `DELETE` rows in this table — corrections go through
`event_overrides` instead, which references the original event and
requires a `reason`.
