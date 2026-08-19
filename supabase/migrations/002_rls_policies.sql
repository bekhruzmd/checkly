-- ============================================================
-- Supabase Row-Level Security policies for Checkly.
-- Self-hosted Supabase: auth.uid() resolves via GoTrue JWT.
--
-- Two roles:
--   owner   — full admin; can manage admin_users, run imports,
--             approve enrollments
--   manager — read/excusal access; cannot manage users or import
--
-- The Python backend (asyncpg, DATABASE_URL) connects as
-- service_role / postgres user — RLS is bypassed for all
-- backend writes. RLS only applies to the Supabase JS client
-- used by the admin dashboard frontend.
-- ============================================================

-- Helper: is the caller a known admin (any role)?
CREATE OR REPLACE FUNCTION is_admin() RETURNS boolean
    LANGUAGE sql SECURITY DEFINER STABLE
    AS $$
        SELECT EXISTS (
            SELECT 1 FROM admin_users WHERE auth_uid = auth.uid()
        );
    $$;

-- Helper: is the caller an owner?
CREATE OR REPLACE FUNCTION is_owner() RETURNS boolean
    LANGUAGE sql SECURITY DEFINER STABLE
    AS $$
        SELECT EXISTS (
            SELECT 1 FROM admin_users WHERE auth_uid = auth.uid() AND role = 'owner'
        );
    $$;


-- ── workers ───────────────────────────────────────────────────────────────────

ALTER TABLE workers ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_workers" ON workers
    FOR SELECT TO authenticated
    USING (is_admin());

-- All writes go through the Python backend (service_role), never the JS client.


-- ── managers ─────────────────────────────────────────────────────────────────

ALTER TABLE managers ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_managers" ON managers
    FOR SELECT TO authenticated
    USING (is_admin());


-- ── admin_users ───────────────────────────────────────────────────────────────

ALTER TABLE admin_users ENABLE ROW LEVEL SECURITY;

-- Any admin can read the list (needed to show who approved an enrollment).
CREATE POLICY "admins_read_admin_users" ON admin_users
    FOR SELECT TO authenticated
    USING (is_admin());

-- Only owners can add or remove admins.
CREATE POLICY "owner_manage_admin_users" ON admin_users
    FOR ALL TO authenticated
    USING (is_owner())
    WITH CHECK (is_owner());


-- ── attendance_events ─────────────────────────────────────────────────────────

ALTER TABLE attendance_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_attendance" ON attendance_events
    FOR SELECT TO authenticated
    USING (is_admin());

-- No INSERT/UPDATE/DELETE via JS client — only the Python backend writes here.


-- ── event_overrides ───────────────────────────────────────────────────────────

ALTER TABLE event_overrides ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_overrides" ON event_overrides
    FOR SELECT TO authenticated
    USING (is_admin());

CREATE POLICY "admins_write_overrides" ON event_overrides
    FOR INSERT TO authenticated
    WITH CHECK (is_admin());


-- ── biometric_consents ────────────────────────────────────────────────────────

ALTER TABLE biometric_consents ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_consents" ON biometric_consents
    FOR SELECT TO authenticated
    USING (is_admin());

-- Inserts only via Python backend; no JS-client write policy.


-- ── enrollment_sessions ───────────────────────────────────────────────────────

ALTER TABLE enrollment_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_sessions" ON enrollment_sessions
    FOR SELECT TO authenticated
    USING (is_admin());

-- Managers can read the enrollment queue; writes go through the Python backend.


-- ── desk_zones ────────────────────────────────────────────────────────────────

ALTER TABLE desk_zones ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_zones" ON desk_zones
    FOR SELECT TO authenticated
    USING (is_admin());

CREATE POLICY "owner_manage_zones" ON desk_zones
    FOR ALL TO authenticated
    USING (is_owner())
    WITH CHECK (is_owner());


-- ── desk_presence_events ──────────────────────────────────────────────────────

ALTER TABLE desk_presence_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_presence_events" ON desk_presence_events
    FOR SELECT TO authenticated
    USING (is_admin());


-- ── shift_leave_summary ───────────────────────────────────────────────────────

ALTER TABLE shift_leave_summary ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_leave_summary" ON shift_leave_summary
    FOR SELECT TO authenticated
    USING (is_admin());


-- ── presence_checks ───────────────────────────────────────────────────────────

ALTER TABLE presence_checks ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_presence_checks" ON presence_checks
    FOR SELECT TO authenticated
    USING (is_admin());


-- ── admin_audit_log ───────────────────────────────────────────────────────────

ALTER TABLE admin_audit_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "admins_read_audit_log" ON admin_audit_log
    FOR SELECT TO authenticated
    USING (is_admin());


-- ── shifts / schedules / daily_summary (read-only for all admins) ─────────────

ALTER TABLE shifts ENABLE ROW LEVEL SECURITY;
CREATE POLICY "admins_read_shifts" ON shifts
    FOR SELECT TO authenticated USING (is_admin());

ALTER TABLE schedules ENABLE ROW LEVEL SECURITY;
CREATE POLICY "admins_read_schedules" ON schedules
    FOR SELECT TO authenticated USING (is_admin());

ALTER TABLE daily_summary ENABLE ROW LEVEL SECURITY;
CREATE POLICY "admins_read_daily_summary" ON daily_summary
    FOR SELECT TO authenticated USING (is_admin());

ALTER TABLE shift_monitoring_config ENABLE ROW LEVEL SECURITY;
CREATE POLICY "admins_read_monitoring_config" ON shift_monitoring_config
    FOR SELECT TO authenticated USING (is_admin());

ALTER TABLE office_locations ENABLE ROW LEVEL SECURITY;
CREATE POLICY "admins_read_office_locations" ON office_locations
    FOR SELECT TO authenticated USING (is_admin());
