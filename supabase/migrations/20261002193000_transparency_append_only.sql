-- S-P1-3: enforce append-only on the transparency tables at the database layer.
--
-- The transparency log and the checkpoint table were append-only by
-- convention: UNIQUE(index) but no UPDATE/DELETE trigger, and the app
-- connects as the table owner (bypassing RLS), so a DELETE plus a fresh
-- checkpoint silently laundered a truncation into a consistent signed
-- history -- defeating the package's headline property ("an entry that can
-- be edited after the fact is the whole bug this package exists to
-- prevent", src/transparency/log.py).
--
-- This migration makes mutation structurally impossible, in two layers:
--
--   1. A BEFORE trigger on both tables raises on any UPDATE, DELETE, or
--      TRUNCATE. Triggers fire for the table owner too, which is the whole
--      point: REVOKE cannot constrain the owner, so the trigger is the
--      enforcement that bites every role.
--   2. REVOKE UPDATE/DELETE/TRUNCATE on both tables from anon,
--      authenticated, PUBLIC, and service_role, so no lesser role can mutate
--      the tables even if a trigger were dropped during maintenance.
--      Nothing in the codebase updates or deletes these rows: checkpoints
--      are saved with INSERT only (src/transparency/store.py), and log
--      entries are appended, never rewritten.
--
-- Idempotent: safe to run more than once. Applied by scripts/migrate.py,
-- oldest first, like every other file in this directory (the schema
-- authority).
--
-- Operational note: the trigger also blocks the tamper-simulation UPDATE in
-- tests/test_proof_permalinks.py if that test ever runs against Postgres
-- instead of its usual SQLite fixture. That is the trigger working as
-- intended; the test documents the state the page must render when the
-- stored payload disagrees with its leaf hash, which the trigger now makes
-- unreachable through SQL.

CREATE OR REPLACE FUNCTION transparency_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'transparency log is append-only: % on % is forbidden', TG_OP, TG_TABLE_NAME;
END;
$$;

DO $$
BEGIN
    IF to_regclass('public.merkle_log_entries') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_merkle_log_entries_no_mutation ON merkle_log_entries;
        CREATE TRIGGER trg_merkle_log_entries_no_mutation
            BEFORE UPDATE OR DELETE OR TRUNCATE ON merkle_log_entries
            FOR EACH STATEMENT
            EXECUTE FUNCTION transparency_reject_mutation();
    END IF;
    IF to_regclass('public.transparency_checkpoints') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_transparency_checkpoints_no_mutation ON transparency_checkpoints;
        CREATE TRIGGER trg_transparency_checkpoints_no_mutation
            BEFORE UPDATE OR DELETE OR TRUNCATE ON transparency_checkpoints
            FOR EACH STATEMENT
            EXECUTE FUNCTION transparency_reject_mutation();
    END IF;
END
$$;

-- Belt and suspenders: strip the mutation privileges from every non-owner
-- role that might hold them. Guarded per role so the migration also applies
-- on a database without the Supabase roles.
DO $$
DECLARE
    tbl text;
    rol text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY['merkle_log_entries', 'transparency_checkpoints'] LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM PUBLIC', tbl);
        FOREACH rol IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rol) THEN
                EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM %I', tbl, rol);
            END IF;
        END LOOP;
    END LOOP;
END
$$;
