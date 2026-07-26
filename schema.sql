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
