-- v2 transparency checkpoint signer: the columns, the alert table, and the
-- least-privilege role the signer runs as.
--
-- Three things land here, all additive, all idempotent.
--
-- 1. Columns on transparency_checkpoints for the v2 signed format.
--    checkpoint_format  'n1-json-v1' or 'c2sp-tlog-checkpoint-v2'. Existing rows
--                       get 'n1-json-v1' written back, so to_signed() can trust
--                       the column rather than inferring the format from the
--                       absence of new data.
--    key_name           the C2SP signed-note key name a verifier looks the
--                       public key up under. Null on v1 rows: v1 has no note
--                       form, so there is nothing to look up.
--    previous_digest    hex sha256 over the previous signed checkpoint's
--                       signing bytes and signature (checkpoint_digest()). This
--                       is the chain a reader walks, and it is what makes the
--                       ordering of published checkpoints checkable without the
--                       database. Null on the first v2 checkpoint and on v1.
--    log_id             which log the row covers, matching the note's origin.
--                       A second log is a second value here rather than a
--                       second table.
--
-- 2. UNIQUE on (tree_size, signed format). One published checkpoint per size per
--    format is the property that makes equivocation detectable at all: with
--    duplicates allowed, the signer would have to choose which root to compare
--    against, and a second row with a different root would be indistinguishable
--    from a normal re-run.
--
--    The format is part of the key because v1 rows are append-only and cannot be
--    rewritten: the first v2 checkpoint is necessarily published at the same tree
--    size as the v1 checkpoint it follows, chained to it by previous_digest.
--    Uniqueness on tree_size alone would forbid exactly the row the format
--    migration exists to write, and the signer would then report already_signed
--    on every run against an HMAC-signed row.
--
--    coalesce() rather than the bare column: the column is nullable for v1 rows
--    and NULLs compare as distinct in a Postgres unique index, which would leave
--    a pre-backfill row unprotected. 'n1-json-v1' is what the backfill above
--    writes, so this is the same key it already occupies.
--
--    (Had two rows at one size genuinely existed for one format, this statement
--    would fail loudly rather than silently pick a winner, which is the correct
--    outcome -- those rows would need a human to reconcile.)
--
-- 3. transparency_alerts: an append-only log of signer refusals, so a refusal
--    is visible to a human on /healthz/details instead of only to the cron
--    request nobody reads. Same mutation trigger as the other two tables, plus
--    INSERT-only grants.
--
-- 4. transparency_signer, a NOLOGIN group role with the narrowest grants that
--    can still do the job: read the log, insert checkpoints, insert alerts. No
--    UPDATE or DELETE anywhere (not merely revoked -- never granted), no access
--    to raw_articles or anything else in the schema. NOLOGIN on purpose: this
--    role holds no password and cannot be used to connect. The ceremony step is
--    to create a LOGIN role, grant transparency_signer TO it, and put the
--    password in a secret store -- never in this migration, never in the repo.
--
-- Idempotent: every statement is guarded, so re-running is a no-op. Applied by
-- scripts/migrate.py, oldest first.

DO $$
BEGIN
    IF to_regclass('public.transparency_checkpoints') IS NULL THEN
        RETURN;
    END IF;
    ALTER TABLE transparency_checkpoints
        ADD COLUMN IF NOT EXISTS checkpoint_format text,
        ADD COLUMN IF NOT EXISTS key_name text,
        ADD COLUMN IF NOT EXISTS previous_digest text,
        ADD COLUMN IF NOT EXISTS log_id text;

    -- Backfill: a row that predates this migration is v1 by definition.
    --
    -- This is the one statement in the whole schema that mutates an existing
    -- transparency row, and the append-only trigger from 20261002193000 exists
    -- precisely to forbid it. So the trigger is stood down for exactly this
    -- backfill and put straight back, inside a subtransaction: if the UPDATE
    -- raises, the DISABLE rolls back with it and the table is never left
    -- mutable. Everything after the backfill -- and every migration that comes
    -- later -- still hits the armed trigger.
    BEGIN
        ALTER TABLE transparency_checkpoints
            DISABLE TRIGGER trg_transparency_checkpoints_no_mutation;
        UPDATE transparency_checkpoints
            SET checkpoint_format = 'n1-json-v1'
            WHERE checkpoint_format IS NULL;
    EXCEPTION WHEN insufficient_privilege THEN
        -- Not the table owner. The columns are still added and the index below
        -- still builds; the backfill is left for a human to run deliberately.
        RAISE WARNING 'transparency_checkpoints: cannot disable append-only '
            'trigger, skipping checkpoint_format backfill (%)', SQLERRM;
    END;
    ALTER TABLE transparency_checkpoints
        ENABLE TRIGGER trg_transparency_checkpoints_no_mutation;

    COMMENT ON COLUMN transparency_checkpoints.checkpoint_format IS
        'Signed format: n1-json-v1 (pre-2026-10-02) or c2sp-tlog-checkpoint-v2';
    COMMENT ON COLUMN transparency_checkpoints.key_name IS
        'C2SP signed-note key name; null on v1 rows, which have no note form';
    COMMENT ON COLUMN transparency_checkpoints.previous_digest IS
        'checkpoint_digest() of the previously published checkpoint; forms the chain';
END
$$;

DO $$
BEGIN
    IF to_regclass('public.transparency_checkpoints') IS NULL THEN
        RETURN;
    END IF;
    DROP INDEX IF EXISTS uq_transparency_checkpoints_tree_size;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_transparency_checkpoints_size_format
        ON transparency_checkpoints (tree_size, coalesce(checkpoint_format, 'n1-json-v1'));
END
$$;

-- The alert table. detail is jsonb so a refusal carries its reason codes and
-- root prefixes structurally, and so the model in src/transparency/alerts.py
-- binds to it without a cast.
DO $$
BEGIN
    IF to_regclass('public.transparency_alerts') IS NULL THEN
        CREATE TABLE transparency_alerts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind text NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        );
    END IF;
    CREATE INDEX IF NOT EXISTS ix_transparency_alerts_kind_created
        ON transparency_alerts (kind, created_at);
END
$$;

-- Append-only enforcement, matching 20261002193000: the trigger is what bites
-- the table owner, REVOKE is what stops a lesser role.
DO $$
BEGIN
    IF to_regclass('public.transparency_alerts') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_transparency_alerts_no_mutation ON transparency_alerts;
        CREATE TRIGGER trg_transparency_alerts_no_mutation
            BEFORE UPDATE OR DELETE OR TRUNCATE ON transparency_alerts
            FOR EACH STATEMENT
            EXECUTE FUNCTION transparency_reject_mutation();
    END IF;
END
$$;

DO $$
DECLARE
    rol text;
BEGIN
    IF to_regclass('public.transparency_alerts') IS NULL THEN
        RETURN;
    END IF;
    REVOKE UPDATE, DELETE, TRUNCATE ON transparency_alerts FROM PUBLIC;
    FOREACH rol IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rol) THEN
            EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON transparency_alerts FROM %I', rol);
        END IF;
    END LOOP;
END
$$;

-- Least-privilege signer role.
--
-- NOLOGIN: this is a group, not a credential. A role that cannot log in cannot
-- be used directly to reach the database, so the only way to get its grants is
-- to be explicitly granted membership at the key ceremony -- which is a
-- deliberate human step, not a side effect of a migration.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'transparency_signer') THEN
        CREATE ROLE transparency_signer NOLOGIN;
    END IF;
END
$$;

DO $$
DECLARE
    readable text;
    writable text;
BEGIN
    FOREACH readable IN ARRAY ARRAY['merkle_log_entries', 'raw_articles'] LOOP
        IF to_regclass('public.' || readable) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('GRANT SELECT ON %I TO transparency_signer', readable);
        EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON %I FROM transparency_signer', readable);
    END LOOP;

    -- Checkpoints and alerts are insert-only: never granted anything else.
    FOREACH writable IN ARRAY ARRAY['transparency_checkpoints', 'transparency_alerts'] LOOP
        IF to_regclass('public.' || writable) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('GRANT INSERT ON %I TO transparency_signer', writable);
        EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM transparency_signer', writable);
    END LOOP;

    -- USAGE on the schema, or the table grants above are inert.
    EXECUTE 'GRANT USAGE ON SCHEMA public TO transparency_signer';
END
$$;
