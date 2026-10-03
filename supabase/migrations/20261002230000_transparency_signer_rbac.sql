-- transparency_signer: make the grants it already has actually usable, and make
-- them the only ones.
--
-- 20261002200000 gave transparency_signer a grant list that looked right and was
-- dead on arrival, because it was written from the table list and never from the
-- code path. Measured on dev as the role itself (SET ROLE transparency_signer,
-- every statement in its own savepoint):
--
--   SELECT merkle_log_entries        -> 0 rows   (owner sees 13)
--   SELECT transparency_checkpoints  -> permission denied for table
--   SELECT transparency_alerts       -> permission denied for table
--   INSERT transparency_checkpoints  -> new row violates row-level security policy
--   INSERT transparency_alerts       -> new row violates row-level security policy
--   SELECT raw_articles              -> 0 rows
--
-- Two independent causes, and the second one is not in the original ticket:
--
-- 1. Missing SELECT on the two tables the signer writes to. The signer is not
--    write-only. signing.py reads transparency_checkpoints on every run:
--    _newest_checkpoint() is the "refuse to go backwards" check, the
--    previous_digest chain link, and the dead-man's-switch input;
--    _checkpoints_at_size() is the equivocation check, the one thing that makes
--    a public transparency log worth having. The watchdog route reads
--    transparency_alerts through the same least-privilege DSN
--    (curation_ui/cron.py: _database_url), so a human only sees refusals if the
--    signer role can read them. A signer that cannot read the log it is signing
--    signs nothing and reports nothing.
--
-- 2. RLS with no policies. 20261002220000 enabled row-level security on every
--    public table -- correctly, that is the fix for the anon-key leak -- and
--    that turned every table grant on this role into a no-op: with RLS on and
--    pg_policies empty, a non-owner role sees zero rows and cannot insert a row,
--    no matter what it is granted. So the INSERT grants from 20261002200000 were
--    equally inert: the role could not publish a checkpoint or record a single
--    alert. Granting SELECT alone would have left the feature just as dead.
--
-- So this migration does three things.
--
-- a) Grants, exactly and only these:
--      SELECT  merkle_log_entries        derive the root, re-derive the prefix
--      INSERT  merkle_log_entries        never: the log is appended by ingest
--      SELECT  transparency_checkpoints  the chain, the equivocation check, the watchdog
--      INSERT  transparency_checkpoints  publishing
--      SELECT  transparency_alerts       the watchdog reads refusals
--      INSERT  transparency_alerts       recording refusals
--    and REVOKE ALL on raw_articles, which 20261002200000 granted. The signer
--    needs no article row: leaf hashing covers the content hash, and the content
--    hash is already inside the log payload (src/ingestion/rss_evidence.py builds
--    the payload from the article's own hash, and never from the article). The
--    grant was an accident of looping over two table names; it was also inert,
--    since raw_articles has RLS on and no policy.
--
-- b) Five policies, each keyed TO transparency_signer, so they widen nothing for
--    anyone else -- anon, authenticated and service_role are unaffected by their
--    existence, which is what keeps the 20261002220000 fix closed.
--
--    USING (true) / WITH CHECK (true) is deliberate, and the alternative is
--    wrong rather than merely looser. The signer has to see the WHOLE log: a
--    root is over all leaves [0, tree_size), and the equivocation check
--    re-derives the root over the prefix an earlier checkpoint already covers.
--    Any predicate narrower than "everything" -- a time window, an index range,
--    a hash prefix -- would let the role derive a root over a set of leaves that
--    is not the log, and it would sign that root as the log's. That is the exact
--    split-brain the signer exists to prevent, introduced through the RLS layer
--    where nobody would look for it. The narrowing that matters is `TO
--    transparency_signer`, which is what makes the policy invisible to every
--    other role. The write policies are the same argument from the other side:
--    a checkpoint row's contents are the signer's own signature, and an alert's
--    detail is the refusal the caller already decided to publish, so there is no
--    subset of rows the role should be allowed to write and no predicate worth
--    the maintenance of inventing one.
--
-- c) Re-arms ENABLE ROW LEVEL SECURITY on all three tables, so this migration
--    cannot be the reason a future one leaves them off. The F4 anon leak was
--    exactly that class of omission.
--
-- Idempotent: every grant is re-issued, every policy is dropped before it is
-- created, every ENABLE is a no-op on re-run. Applied by scripts/migrate.py.

-- The role itself, if it is missing (a database that predates 20261002200000).
-- Never dropped: dev carries a durable (postgres, admin_option=True) membership
-- from an in-transaction grant the pooler made permanent, so the role has
-- dependent objects and must be left alone.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'transparency_signer') THEN
        CREATE ROLE transparency_signer NOLOGIN;
    END IF;
END
$$;

-- Grants. Read-only on the log; read/insert on the two transparency tables; no
-- reach into anything else in the schema.
DO $$
DECLARE
    readable text;
    writable text;
    forbidden text;
BEGIN
    FOREACH readable IN ARRAY ARRAY['merkle_log_entries', 'transparency_checkpoints',
                                     'transparency_alerts'] LOOP
        IF to_regclass('public.' || readable) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('GRANT SELECT ON %I TO transparency_signer', readable);
        -- Explicit revokes, not just the absence of a grant: a role that once
        -- held more must be able to re-run this file and end up narrower.
        EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER '
                       'ON %I FROM transparency_signer', readable);
    END LOOP;

    FOREACH writable IN ARRAY ARRAY['transparency_checkpoints', 'transparency_alerts'] LOOP
        IF to_regclass('public.' || writable) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('GRANT INSERT ON %I TO transparency_signer', writable);
        EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER '
                       'ON %I FROM transparency_signer', writable);
    END LOOP;

    -- Everything else: no grant at all. raw_articles was granted by
    -- 20261002200000 and is not needed; the rest are named so a future grant to
    -- this role on a neighbouring table is revoked rather than inherited.
    FOREACH forbidden IN ARRAY ARRAY['raw_articles', 'stories', 'reporting_units', 'events'] LOOP
        IF to_regclass('public.' || forbidden) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('REVOKE ALL ON %I FROM transparency_signer', forbidden);
    END LOOP;

    -- USAGE on the schema, or the table grants above are inert.
    EXECUTE 'GRANT USAGE ON SCHEMA public TO transparency_signer';
END
$$;

-- Policies. One per (table, command) the role actually has, each keyed to the
-- role. ENABLE ROW LEVEL SECURITY is re-issued so the grants above are never
-- inert again.
DO $$
BEGIN
    IF to_regclass('public.merkle_log_entries') IS NOT NULL THEN
        ALTER TABLE merkle_log_entries ENABLE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS transparency_signer_read ON merkle_log_entries;
        CREATE POLICY transparency_signer_read ON merkle_log_entries
            FOR SELECT TO transparency_signer USING (true);
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('public.transparency_checkpoints') IS NOT NULL THEN
        ALTER TABLE transparency_checkpoints ENABLE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS transparency_signer_read ON transparency_checkpoints;
        CREATE POLICY transparency_signer_read ON transparency_checkpoints
            FOR SELECT TO transparency_signer USING (true);
        DROP POLICY IF EXISTS transparency_signer_insert ON transparency_checkpoints;
        CREATE POLICY transparency_signer_insert ON transparency_checkpoints
            FOR INSERT TO transparency_signer WITH CHECK (true);
    END IF;
END
$$;

DO $$
BEGIN
    IF to_regclass('public.transparency_alerts') IS NOT NULL THEN
        ALTER TABLE transparency_alerts ENABLE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS transparency_signer_read ON transparency_alerts;
        CREATE POLICY transparency_signer_read ON transparency_alerts
            FOR SELECT TO transparency_signer USING (true);
        DROP POLICY IF EXISTS transparency_signer_insert ON transparency_alerts;
        CREATE POLICY transparency_signer_insert ON transparency_alerts
            FOR INSERT TO transparency_signer WITH CHECK (true);
    END IF;
END
$$;
