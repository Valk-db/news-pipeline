-- Row level security backfill: close the two public tables that were left open.
--
-- Every other table in this schema has carried "ALTER TABLE ... ENABLE ROW LEVEL
-- SECURITY" since the core schema migration, and no table has a single RLS
-- policy. That combination is deliberate, not an oversight: the application only
-- ever reaches this database as `postgres`, which is the table owner AND carries
-- BYPASSRLS (so it is unaffected by RLS either way), while the anonymous PostgREST
-- role -- which is handed out with the frontend build and is therefore public --
-- is denied everything. RLS with no policy is the blanket deny that makes the
-- whole database server-side only.
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
-- anonymous REST path. Explicitly NOT FORCE ROW LEVEL SECURITY, and the reason
-- is measured rather than assumed.
--
-- FORCE would additionally constrain the table OWNER. It buys nothing observable
-- here because the role the application actually connects as carries BYPASSRLS,
-- and `rolbypassrls` outranks both ENABLE and FORCE: it is true for `postgres`,
-- and `postgres` is both the role the app's DATABASE_URL resolves to and the
-- owner of all 35 public tables. A BYPASSRLS role is exempt from row security
-- whether or not the table is forced, so FORCE changes nothing today.
--
-- An earlier version of this comment justified the omission by saying the
-- application connects as `service_role` "rather than as the owner". That was
-- wrong, and wrong in the half that mattered: the connecting role is `postgres`,
-- which IS the owner, so "FORCE would constrain it" was a real concern rather
-- than a non-issue. It came out harmless for the wrong reason -- that role also
-- carries BYPASSRLS -- and the correct reason is the stronger one above: a
-- role exempt from row security outright is precisely the role FORCE cannot
-- reach.
-- `tests/test_rls_migration_rationale.py` pins both halves so this paragraph
-- cannot rot back into the claim it replaced.
--
-- The blanket deny this schema gets does NOT come from FORCE, and not from any
-- policy either. It comes from `ENABLE ROW LEVEL SECURITY` with zero policies:
-- measured on dev, 31 of the 35 public tables are (rls on, forced off, 0
-- policies), and all 4 that do carry a policy scope it to `transparency_signer`
-- alone, so no `anon` or `authenticated` read has a policy to satisfy. That
-- state is the entire reason this database is server-side only, and extending it
-- to tables that missed the line is what this migration is for.

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