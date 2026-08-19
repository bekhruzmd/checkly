-- ============================================================
-- Checkly — combined Supabase/Postgres schema
-- Self-hosted Supabase on your own VPS. NOT Supabase cloud.
-- Biometric data stays on-premises per Uzbekistan localization
-- requirements (personal data law, Art. 8 special categories).
--
-- Run this against a fresh database. If migrating from the old
-- schema.sql + desk_presence_schema.sql, see the comment at the
-- bottom of this file.
--
-- No worker photos are ever stored — only 512-d ArcFace embeddings.
-- Hash-chained audit tables (attendance_events, presence_checks,
-- desk_presence_events, biometric_consents) must never be modified
-- via UPDATE/DELETE in application code. All corrections go through
-- their respective override/override-comment tables.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ── Shifts ────────────────────────────────────────────────────────────────────

CREATE TABLE shifts (
    id                  SERIAL PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    start_time          TIME NOT NULL,
    end_time            TIME NOT NULL,
    grace_period_min    INT NOT NULL DEFAULT 5,
    fee_per_min         NUMERIC(10,2) NOT NULL DEFAULT 0.50
);

INSERT INTO shifts (name, start_time, end_time, grace_period_min, fee_per_min) VALUES
    ('morning', '09:00', '17:00', 5, 0.50),
    ('night',   '21:00', '05:00', 5, 0.50);

-- ── Managers (referenced by enrollment, overrides, presence excusals) ─────────

CREATE TABLE managers (
    id               SERIAL PRIMARY KEY,
    full_name        TEXT NOT NULL,
    telegram_chat_id BIGINT UNIQUE,
    active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Supabase auth → role mapping ──────────────────────────────────────────────
-- Links a Supabase auth.users row to a manager record + role.
-- role 'owner'   — can manage other admins, approve enrollments, run imports
-- role 'manager' — can view dashboard, trigger presence excusals
-- Populated manually by an owner after adding the user via Supabase Auth.

CREATE TABLE admin_users (
    id          SERIAL PRIMARY KEY,
    auth_uid    UUID NOT NULL UNIQUE,       -- auth.users.id from Supabase Auth
    manager_id  INT REFERENCES managers(id),
    role        TEXT NOT NULL CHECK (role IN ('owner', 'manager')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Workers ───────────────────────────────────────────────────────────────────
-- face_embedding is nullable: workers created via Workly import or manually
-- have no embedding until enrollment completes. Check-in only matches workers
-- with face_enrolled = TRUE and a non-null embedding.
--
-- employee_id is the stable external key from Workly (or any other HR system)
-- used for idempotent roster imports. NULL for manually-created workers.

CREATE TABLE workers (
    id               SERIAL PRIMARY KEY,
    full_name        TEXT NOT NULL,
    employee_id      TEXT UNIQUE,           -- Workly stable ID; NULL if manual
    telegram_chat_id BIGINT UNIQUE,
    face_embedding   FLOAT8[],              -- nullable; set only after enrollment
    face_enrolled    BOOLEAN NOT NULL DEFAULT FALSE,
    shift            TEXT NOT NULL CHECK (shift IN ('morning', 'night')),
    department       TEXT,
    position         TEXT,
    active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Per-worker schedule assignments ──────────────────────────────────────────

CREATE TABLE schedules (
    id              SERIAL PRIMARY KEY,
    worker_id       INT NOT NULL REFERENCES workers(id),
    shift_id        INT NOT NULL REFERENCES shifts(id),
    effective_from  DATE NOT NULL DEFAULT CURRENT_DATE,
    effective_to    DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT no_overlap EXCLUDE USING gist (
        worker_id WITH =,
        daterange(effective_from, COALESCE(effective_to, '9999-12-31'::date), '[)') WITH &&
    )
);

CREATE INDEX idx_schedules_worker ON schedules(worker_id);
CREATE INDEX idx_schedules_active ON schedules(worker_id) WHERE effective_to IS NULL;

-- ── Biometric consent ─────────────────────────────────────────────────────────
-- Must exist for a worker BEFORE any embedding is generated.
-- Append-only, hash-chained. Consent text is the exact string displayed
-- to and acknowledged by the worker at enrollment time.

CREATE TABLE biometric_consents (
    id              BIGSERIAL PRIMARY KEY,
    worker_id       INT NOT NULL REFERENCES workers(id),
    consented_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    consent_text    TEXT NOT NULL,          -- exact text shown and agreed to
    witnessed_by    INT REFERENCES managers(id),
    prev_hash       TEXT,
    row_hash        TEXT NOT NULL
);

CREATE INDEX idx_bc_worker ON biometric_consents(worker_id);

-- ── Enrollment sessions ───────────────────────────────────────────────────────
-- Manager starts a session for a worker at a specific camera zone.
-- The edge device detects the worker's face and submits embeddings.
-- candidate_embedding is updated as a running average of submitted samples.
-- Status transitions:  active → ready (min_samples reached) → approved | rejected
--
-- Edge device only submits samples while status = 'active'.
-- Manager approves when status = 'ready' (can also approve with fewer samples
-- by setting override = TRUE in the approve payload — for edge cases).

CREATE TABLE enrollment_sessions (
    id                   BIGSERIAL PRIMARY KEY,
    worker_id            INT NOT NULL REFERENCES workers(id),
    consent_id           BIGINT NOT NULL REFERENCES biometric_consents(id),
    desk_zone_id         INT NOT NULL,      -- FK added after desk_zones table
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_by           INT REFERENCES managers(id),
    status               TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'ready', 'approved', 'rejected')),
    sample_count         INT NOT NULL DEFAULT 0,
    min_samples          INT NOT NULL DEFAULT 5,
    candidate_embedding  FLOAT8[],          -- running average of submitted samples
    approved_by          INT REFERENCES managers(id),
    approved_at          TIMESTAMPTZ,
    approval_note        TEXT,
    completed_at         TIMESTAMPTZ
);

CREATE INDEX idx_es_worker ON enrollment_sessions(worker_id);
CREATE INDEX idx_es_status ON enrollment_sessions(status);

-- ── Office locations ──────────────────────────────────────────────────────────

CREATE TABLE office_locations (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    latitude        FLOAT8 NOT NULL,
    longitude       FLOAT8 NOT NULL,
    radius_meters   INT NOT NULL DEFAULT 150
);

-- ── Attendance events (append-only, hash-chained) ────────────────────────────

CREATE TABLE attendance_events (
    id                      BIGSERIAL PRIMARY KEY,
    worker_id               INT NOT NULL REFERENCES workers(id),
    event_type              TEXT NOT NULL CHECK (event_type IN ('check_in', 'check_out')),
    server_timestamp        TIMESTAMPTZ NOT NULL DEFAULT now(),
    match_confidence        FLOAT8 NOT NULL,
    match_status            TEXT NOT NULL CHECK (match_status IN ('accepted', 'manual_review', 'rejected')),
    liveness_passed         BOOLEAN NOT NULL,
    latitude                FLOAT8,
    longitude               FLOAT8,
    distance_from_office_m  FLOAT8,
    mock_location_flag      BOOLEAN NOT NULL DEFAULT FALSE,
    minutes_late            INT,
    fee_charged             NUMERIC(10,2) NOT NULL DEFAULT 0,
    prev_hash               TEXT,
    row_hash                TEXT NOT NULL
);

CREATE INDEX idx_events_worker    ON attendance_events(worker_id);
CREATE INDEX idx_events_timestamp ON attendance_events(server_timestamp);

-- Supabase Realtime needs REPLICA IDENTITY FULL to stream all columns.
ALTER TABLE attendance_events REPLICA IDENTITY FULL;

CREATE TABLE event_overrides (
    id              SERIAL PRIMARY KEY,
    event_id        BIGINT NOT NULL REFERENCES attendance_events(id),
    reviewed_by     TEXT NOT NULL,
    reason          TEXT NOT NULL,
    new_fee         NUMERIC(10,2),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Desk zones ────────────────────────────────────────────────────────────────

CREATE TABLE desk_zones (
    id                  SERIAL PRIMARY KEY,
    desk_label          TEXT NOT NULL,
    camera_id           TEXT NOT NULL,
    zone_type           TEXT NOT NULL CHECK (zone_type IN ('desk', 'safe_zone')),
    zone_polygon        JSONB NOT NULL,
    role_category       TEXT,
    leave_threshold     INT NOT NULL DEFAULT 40,
    min_leave_seconds   INT NOT NULL DEFAULT 60,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Add FK from enrollment_sessions to desk_zones now that desk_zones exists.
ALTER TABLE enrollment_sessions
    ADD CONSTRAINT fk_es_desk_zone FOREIGN KEY (desk_zone_id) REFERENCES desk_zones(id);

-- ── Desk-presence events (append-only, hash-chained) ─────────────────────────

CREATE TABLE desk_presence_events (
    id              BIGSERIAL PRIMARY KEY,
    desk_zone_id    INT NOT NULL REFERENCES desk_zones(id),
    worker_id       INT REFERENCES workers(id),
    event_type      TEXT NOT NULL CHECK (event_type IN ('occupied_start', 'occupied_end')),
    event_ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    confidence      FLOAT8,
    prev_hash       TEXT,
    event_hash      TEXT NOT NULL
);

CREATE INDEX idx_dpe_zone   ON desk_presence_events(desk_zone_id);
CREATE INDEX idx_dpe_worker ON desk_presence_events(worker_id);
CREATE INDEX idx_dpe_ts     ON desk_presence_events(event_ts);

ALTER TABLE desk_presence_events REPLICA IDENTITY FULL;

-- ── Shift leave summary ───────────────────────────────────────────────────────

CREATE TABLE shift_leave_summary (
    id                      SERIAL PRIMARY KEY,
    worker_id               INT NOT NULL REFERENCES workers(id),
    summary_date            DATE NOT NULL,
    desk_zone_id            INT NOT NULL REFERENCES desk_zones(id),
    leave_count             INT NOT NULL DEFAULT 0,
    authorized_break_count  INT NOT NULL DEFAULT 0,
    total_leave_seconds     INT NOT NULL DEFAULT 0,
    threshold_at_time       INT NOT NULL,
    fee_applied             BOOLEAN NOT NULL DEFAULT FALSE,
    disputed                BOOLEAN NOT NULL DEFAULT FALSE,
    dispute_note            TEXT,
    UNIQUE (worker_id, summary_date, desk_zone_id)
);

CREATE INDEX idx_sls_worker ON shift_leave_summary(worker_id);
CREATE INDEX idx_sls_date   ON shift_leave_summary(summary_date);

-- ── Shift monitoring config ───────────────────────────────────────────────────

CREATE TABLE shift_monitoring_config (
    shift_id                INT PRIMARY KEY REFERENCES shifts(id),
    idle_threshold_seconds  INT NOT NULL DEFAULT 360,
    response_window_seconds INT NOT NULL DEFAULT 180,
    enabled                 BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO shift_monitoring_config (shift_id, idle_threshold_seconds, response_window_seconds)
SELECT id, 360, 180 FROM shifts;

-- ── Presence checks (partitioned, hash-chained) ───────────────────────────────

CREATE TABLE presence_checks (
    id               BIGSERIAL,
    worker_id        INT NOT NULL REFERENCES workers(id),
    triggered_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    responded_at     TIMESTAMPTZ,
    window_seconds   INT NOT NULL,
    result           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (result IN ('pending', 'passed', 'failed', 'excused')),
    confidence       FLOAT8,
    excused_by       INT REFERENCES managers(id),
    excuse_reason    TEXT,
    excused_at       TIMESTAMPTZ,
    prev_hash        TEXT,
    row_hash         TEXT NOT NULL,
    PRIMARY KEY (id, triggered_at)
) PARTITION BY RANGE (triggered_at);

CREATE TABLE presence_checks_2026_q3
    PARTITION OF presence_checks
    FOR VALUES FROM ('2026-07-01') TO ('2026-10-01');

CREATE TABLE presence_checks_2026_q4
    PARTITION OF presence_checks
    FOR VALUES FROM ('2026-10-01') TO ('2027-01-01');

CREATE TABLE presence_checks_2027_q1
    PARTITION OF presence_checks
    FOR VALUES FROM ('2027-01-01') TO ('2027-04-01');

CREATE INDEX idx_pc_worker    ON presence_checks(worker_id);
CREATE INDEX idx_pc_result    ON presence_checks(result);
CREATE INDEX idx_pc_triggered ON presence_checks(triggered_at);

-- ── Daily summary ─────────────────────────────────────────────────────────────

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

-- ── Admin audit log ───────────────────────────────────────────────────────────
-- Records bulk operations and privileged actions: Workly imports,
-- enrollment approvals, override decisions, etc.
-- Never modify rows here — append only.

CREATE TABLE admin_audit_log (
    id              BIGSERIAL PRIMARY KEY,
    action          TEXT NOT NULL,          -- 'workly_import', 'enrollment_approved', etc.
    performed_by    INT REFERENCES managers(id),
    performed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    details         JSONB NOT NULL DEFAULT '{}',
    affected_count  INT
);

ALTER TABLE admin_audit_log REPLICA IDENTITY FULL;

-- ── Supabase Realtime publication ─────────────────────────────────────────────
-- Enables live updates for the admin dashboard.
-- desk_presence_events: live desk status board
-- attendance_events:    live check-in/out feed
-- enrollment_sessions:  enrollment queue status updates

BEGIN;
    DROP PUBLICATION IF EXISTS supabase_realtime;
    CREATE PUBLICATION supabase_realtime FOR TABLE
        desk_presence_events,
        attendance_events,
        enrollment_sessions,
        admin_audit_log;
COMMIT;

-- ── Migration note ────────────────────────────────────────────────────────────
-- If you ran schema.sql + desk_presence_schema.sql on the old asyncpg DB and
-- need to migrate in-place instead of starting fresh, run this instead:
--
--   ALTER TABLE workers
--     ADD COLUMN IF NOT EXISTS employee_id TEXT UNIQUE,
--     ADD COLUMN IF NOT EXISTS face_enrolled BOOLEAN NOT NULL DEFAULT FALSE,
--     ADD COLUMN IF NOT EXISTS department TEXT,
--     ADD COLUMN IF NOT EXISTS position TEXT,
--     ALTER COLUMN face_embedding DROP NOT NULL;
--
--   -- Then CREATE TABLE statements for:
--   --   biometric_consents, enrollment_sessions, admin_users, admin_audit_log
--   -- (copy from this file)
--
--   -- Add Realtime publication and REPLICA IDENTITY FULL for relevant tables.
