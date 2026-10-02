-- Row level security backfill: close the two public tables that were left open.
--
-- Every other table in this schema has carried "ALTER TABLE ... ENABLE ROW LEVEL
-- SECURITY" since the core schema migration, and no table has a single RLS
-- policy. That combination is deliberate, not an oversight: the application only
-- ever reaches this database as service_role (which carries BYPASSRLS and so is
-- unaffected by RLS), while the anonymous PostgREST role -- which is handed out
-- with the frontend build and is therefore public -- is denied everything. RLS
-- with no policy is the blanket deny that makes the whole database server-side
-- only.
--
-- Two tables were missed, because they were created after the house style was
-- established and their migrations omitted the line. Measured on dev before this
-- file existed, with the project's own anon key over PostgREST:
--
--   GET /rest/v1/gate_decisions             -> HTTP 200, real rows
--   GET /rest/v1/transparency_checkpoints   -> HTTP 200, real rows
--   GET /rest/v1/stories                    -> HTTP 200, []   (RLS closed)
--
-- so this was not a theoretical gap: gate reasons keyed to story ids, and the
-- Merkle chain roots and chain hashes, were anonymously readable.
--
-- The block below closes every public table that is still open rather than
-- naming the two, because the invariant is the useful part: a migration that
-- forgets the line self-heals on the next run instead of quietly reopening the
-- anonymous REST path. Explicitly NOT FORCE ROW LEVEL SECURITY. The table owner
-- is the role the migrations run as, and FORCE would additionally constrain the
-- owner -- which buys nothing here, because the application connects as
-- service_role through BYPASSRLS, not as the owner.

DO $$
DECLARE
    tbl record;
BEGIN
    -- ALTER TABLE takes exactly one table, so this is a loop rather than one
    -- comma-separated statement.
    FOR tbl IN
        SELECT c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relkind = 'r'
           AND NOT c.relrowsecurity
         ORDER BY c.relname
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl.relname);
        RAISE NOTICE 'row level security enabled on %', tbl.relname;
    END LOOP;
END $$;