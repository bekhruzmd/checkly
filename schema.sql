-- ============================================================
-- Attendance MVP schema
-- No worker photos are ever stored. Only a face embedding
-- (a vector of numbers, not reversible to an image) is kept,
-- captured once at enrollment. Check-in photos are processed
-- in memory and discarded immediately after matching.
-- ============================================================

CREATE TABLE workers (
    id              SERIAL PRIMARY KEY,
    full_name       TEXT NOT NULL,
    telegram_chat_id BIGINT UNIQUE,          -- for notifications only
    face_embedding  FLOAT8[] NOT NULL,       -- ArcFace 512-d vector, no image stored
    shift           TEXT NOT NULL CHECK (shift IN ('morning', 'night')),
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE shifts (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,   -- 'morning' / 'night'
    start_time      TIME NOT NULL,          -- e.g. 09:00
    end_time        TIME NOT NULL,          -- e.g. 17:00
    grace_period_min INT NOT NULL DEFAULT 5,
    fee_per_min     NUMERIC(10,2) NOT NULL DEFAULT 0.50
);

CREATE TABLE office_locations (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    latitude        FLOAT8 NOT NULL,
    longitude       FLOAT8 NOT NULL,
    radius_meters   INT NOT NULL DEFAULT 150
);

-- One row per check-in or check-out. Append-only: never UPDATE or DELETE
-- rows here in application code. That's what makes this an audit trail.
CREATE TABLE attendance_events (
    id                  BIGSERIAL PRIMARY KEY,
    worker_id           INT NOT NULL REFERENCES workers(id),
    event_type          TEXT NOT NULL CHECK (event_type IN ('check_in', 'check_out')),
    server_timestamp    TIMESTAMPTZ NOT NULL DEFAULT now(), -- authoritative time, never trust client clock
    match_confidence    FLOAT8 NOT NULL,       -- cosine similarity score
    match_status        TEXT NOT NULL CHECK (match_status IN ('accepted', 'manual_review', 'rejected')),
    liveness_passed     BOOLEAN NOT NULL,
    latitude            FLOAT8,
    longitude            FLOAT8,
    distance_from_office_m FLOAT8,
    mock_location_flag  BOOLEAN NOT NULL DEFAULT FALSE,
    minutes_late        INT,                  -- null for check_out or on-time
    fee_charged         NUMERIC(10,2) NOT NULL DEFAULT 0,
    prev_hash           TEXT,                 -- hash of previous row, for tamper evidence
    row_hash            TEXT NOT NULL         -- hash of this row's content + prev_hash
);

CREATE INDEX idx_events_worker ON attendance_events(worker_id);
CREATE INDEX idx_events_timestamp ON attendance_events(server_timestamp);

-- Manual overrides (disputes, admin corrections) are their own append-only
-- log, never edits to attendance_events itself.
CREATE TABLE event_overrides (
    id              SERIAL PRIMARY KEY,
    event_id        BIGINT NOT NULL REFERENCES attendance_events(id),
    reviewed_by     TEXT NOT NULL,          -- manager name/id, plain text for MVP
    reason          TEXT NOT NULL,
    new_fee         NUMERIC(10,2),          -- null = no fee change, just annotation
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO shifts (name, start_time, end_time, grace_period_min, fee_per_min) VALUES
    ('morning', '09:00', '17:00', 5, 0.50),
    ('night',   '21:00', '05:00', 5, 0.50);

-- ============================================================
-- Per-worker schedule assignments. Cross-referenced by
-- attendance_events to determine which shift rules applied
-- at the time of a given check-in. Workers can change shifts
-- over time; only one active row per worker at any moment
-- (enforced by the no_overlap exclusion constraint).
-- ============================================================

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE schedules (
    id              SERIAL PRIMARY KEY,
    worker_id       INT NOT NULL REFERENCES workers(id),
    shift_id        INT NOT NULL REFERENCES shifts(id),
    effective_from  DATE NOT NULL DEFAULT CURRENT_DATE,
    effective_to    DATE,                              -- NULL = currently active
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT no_overlap EXCLUDE USING gist (
        worker_id WITH =,
        daterange(effective_from, COALESCE(effective_to, '9999-12-31'::date), '[)') WITH &&
    )
);

CREATE INDEX idx_schedules_worker ON schedules(worker_id);
CREATE INDEX idx_schedules_active ON schedules(worker_id) WHERE effective_to IS NULL;

-- ============================================================
-- Managers — referenced by presence_checks.excused_by.
-- event_overrides.reviewed_by stays TEXT for MVP compatibility.
-- ============================================================

CREATE TABLE managers (
    id               SERIAL PRIMARY KEY,
    full_name        TEXT NOT NULL,
    telegram_chat_id BIGINT UNIQUE,
    active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Per-shift monitoring config. One row per shift (morning/night).
-- Seeded on migration; tune night shift independently if
-- low-light selfie accuracy warrants a longer response window.
-- ============================================================

CREATE TABLE shift_monitoring_config (
    shift_id                INT PRIMARY KEY REFERENCES shifts(id),
    idle_threshold_seconds  INT NOT NULL DEFAULT 360,   -- 6 min; range 300–480 is reasonable
    response_window_seconds INT NOT NULL DEFAULT 180,   -- 3 min; consider 240–300 for night shift
    enabled                 BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO shift_monitoring_config (shift_id, idle_threshold_seconds, response_window_seconds)
SELECT id, 360, 180 FROM shifts;

-- ============================================================
-- Presence checks — triggered when idle threshold is crossed.
-- Partitioned by triggered_at; add quarterly partitions via
-- migration before each quarter starts.
--
-- KNOWN RISK (night shift): ArcFace accuracy degrades under
-- low-light conditions typical of night-shift desk selfies
-- (phone flash as sole light source, heavy shadow contrast).
-- The liveness heuristic (Laplacian sharpness > 50.0) may
-- also produce false negatives for flash-lit photos.
-- Do NOT trust night-shift failure rates until you have tested
-- a real sample (~10 selfies) and confirmed the confidence
-- distribution stays above threshold. See shift_monitoring_config
-- for per-shift threshold overrides once data is available.
--
-- KNOWN RISK (Telegram compression): photos sent via Telegram
-- Bot API are JPEG-compressed by Telegram before delivery.
-- Compression can reduce cosine similarity by 0.02–0.05 vs.
-- direct-upload check-ins. Monitor Telegram-sourced confidence
-- scores separately before relying on the same thresholds.
-- ============================================================

CREATE TABLE presence_checks (
    id               BIGSERIAL,
    worker_id        INT NOT NULL REFERENCES workers(id),
    triggered_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    responded_at     TIMESTAMPTZ,
    window_seconds   INT NOT NULL,        -- snapshot of config at trigger time
    result           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (result IN ('pending', 'passed', 'failed', 'excused')),
    confidence       FLOAT8,             -- ArcFace cosine sim; NULL if no response
    excused_by       INT REFERENCES managers(id),
    excuse_reason    TEXT,
    excused_at       TIMESTAMPTZ,
    prev_hash        TEXT,
    row_hash         TEXT NOT NULL,      -- hashed at INSERT over immutable fields only
    PRIMARY KEY (id, triggered_at)
) PARTITION BY RANGE (triggered_at);

CREATE TABLE presence_checks_2026_q3
    PARTITION OF presence_checks
    FOR VALUES FROM ('2026-07-01') TO ('2026-10-01');

CREATE TABLE presence_checks_2026_q4
    PARTITION OF presence_checks
    FOR VALUES FROM ('2026-10-01') TO ('2027-01-01');

CREATE INDEX idx_pc_worker    ON presence_checks (worker_id);
CREATE INDEX idx_pc_result    ON presence_checks (result);
CREATE INDEX idx_pc_triggered ON presence_checks (triggered_at);

-- ============================================================
-- Daily summary — computed by nightly aggregation job.
-- presence_checks_failed and presence_checks_excused are
-- rolled in alongside the existing lateness aggregation.
-- ============================================================

CREATE TABLE daily_summary (
    summary_date              DATE NOT NULL,
    shift                     TEXT NOT NULL CHECK (shift IN ('morning', 'night')),
    total_workers             INT NOT NULL DEFAULT 0,
    checked_in                INT NOT NULL DEFAULT 0,
    on_time                   INT NOT NULL DEFAULT 0,
    late                      INT NOT NULL DEFAULT 0,
    total_minutes_late        INT NOT NULL DEFAULT 0,
    total_fees                NUMERIC(10,2) NOT NULL DEFAULT 0,
    presence_checks_total     INT NOT NULL DEFAULT 0,
    presence_checks_passed    INT NOT NULL DEFAULT 0,
    presence_checks_failed    INT NOT NULL DEFAULT 0,
    presence_checks_excused   INT NOT NULL DEFAULT 0,
    computed_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (summary_date, shift)
);
