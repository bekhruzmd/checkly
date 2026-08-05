-- ============================================================
-- Desk-presence migration (additive — existing tables unchanged)
--
-- Run once against the live database after deploying the new
-- desk_presence_api.py and desk_presence.py modules.
-- ============================================================

-- One row per monitored camera zone.
-- zone_polygon stores normalized [0,1] coordinates so the polygon
-- remains valid if the camera resolution changes.
CREATE TABLE desk_zones (
    id                  SERIAL PRIMARY KEY,
    desk_label          TEXT NOT NULL,
    camera_id           TEXT NOT NULL,          -- matches local edge-device config
    zone_type           TEXT NOT NULL CHECK (zone_type IN ('desk', 'safe_zone')),
    zone_polygon        JSONB NOT NULL,          -- [[x,y], ...] normalized 0-1 coords
    role_category       TEXT,                   -- optional per-role threshold override
    leave_threshold     INT NOT NULL DEFAULT 40,
    min_leave_seconds   INT NOT NULL DEFAULT 60,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only, hash-chained event log for all zone occupancy transitions.
-- Never UPDATE or DELETE rows — the hash chain would break and it would be
-- detectable. worker_id is nullable because the face match may fail
-- (unidentified occupant), especially for occupied_end events.
CREATE TABLE desk_presence_events (
    id              BIGSERIAL PRIMARY KEY,
    desk_zone_id    INT NOT NULL REFERENCES desk_zones(id),
    worker_id       INT REFERENCES workers(id),
    event_type      TEXT NOT NULL CHECK (event_type IN ('occupied_start', 'occupied_end')),
    event_ts        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- server-side only
    confidence      FLOAT8,                              -- face match similarity; NULL for occupied_end
    prev_hash       TEXT,
    event_hash      TEXT NOT NULL
);

CREATE INDEX idx_dpe_zone   ON desk_presence_events (desk_zone_id);
CREATE INDEX idx_dpe_worker ON desk_presence_events (worker_id);
CREATE INDEX idx_dpe_ts     ON desk_presence_events (event_ts);

-- Per worker/day/desk rollup of leave activity.
-- leave_count counts unauthorized leaves only.
-- fee_applied is always written as FALSE here; a separate human/dispute
-- review step flips it to TRUE before any deduction is processed.
CREATE TABLE shift_leave_summary (
    id                      SERIAL PRIMARY KEY,
    worker_id               INT NOT NULL REFERENCES workers(id),
    summary_date            DATE NOT NULL,
    desk_zone_id            INT NOT NULL REFERENCES desk_zones(id),
    leave_count             INT NOT NULL DEFAULT 0,   -- unauthorized only
    authorized_break_count  INT NOT NULL DEFAULT 0,
    total_leave_seconds     INT NOT NULL DEFAULT 0,
    threshold_at_time       INT NOT NULL,             -- snapshot of leave_threshold at first write
    fee_applied             BOOLEAN NOT NULL DEFAULT FALSE,
    disputed                BOOLEAN NOT NULL DEFAULT FALSE,
    dispute_note            TEXT,
    UNIQUE (worker_id, summary_date, desk_zone_id)
);

CREATE INDEX idx_sls_worker ON shift_leave_summary (worker_id);
CREATE INDEX idx_sls_date   ON shift_leave_summary (summary_date);

-- workers.telegram_chat_id already exists; no change needed.
