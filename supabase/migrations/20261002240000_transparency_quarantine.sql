-- transparency_quarantine: the way a poisoned or unverifiable published
-- checkpoint stops blocking the signer, in an append-only table.
--
-- WHY THIS EXISTS
-- ===============
--
-- Two states are now permanently blocking, and neither can be cleared by
-- editing or removing a row, because both tables are append-only by trigger
-- (20261002193000) and the signer role holds no UPDATE or DELETE at all:
--
-- 1. A row planted with an enormous tree_size. Every later run computes
--    tree_size from the log and refuses with log_shrank against the planted
--    row (signing.py REFUSAL_LOG_SMALLER). With append-only there is no
--    supported way to remove it, so one INSERT halts signing permanently.
--    That is the F5 availability finding.
--
-- 2. A pre-v2 row signed with the development HMAC (algorithm
--    'hmac-sha256-dev'). F1 authenticates the previous checkpoint against the
--    published Ed25519 key set, and no public key can verify an HMAC: keys.py
--    refuses to admit an HMAC into the trusted set at all, deliberately,
--    because it would be verification theater on a public proof page. So the
--    dev database's existing v1 rows would refuse every future run once F1
--    lands. That is the honest consequence of F1, not a defect in it -- and it
--    needs a supported way out.
--
-- The design rule from the F4 finding applies to the fix too: a signer that can
-- delete an inconvenient checkpoint is an equivocation primitive. So the answer
-- is NOT a status column and NOT a DELETE. It is a separate table the signer
-- only ever reads, plus an append-only record of who excluded what and why.
--
-- WHY QUARANTINE IS NOT A WAY TO SIGN A SHORTER HISTORY
-- =====================================================
--
-- The signer skips quarantined rows when choosing the previous checkpoint
-- (signing._newest_checkpoint(skip_ids=...)), which looks like it could be used
-- to walk the signer back below what was published. It cannot, because the
-- external head (F2, transparency_signed_head) does not move when a row is
-- quarantined: the run then computes a newest checkpoint below the recorded
-- head and refuses with head_ahead_of_log. Excluding a row from being trusted
-- and accepting a shorter history are different acts, and only the second one
-- needs the outside record.
--
-- SCHEMA CHOICES
-- ==============
--
-- checkpoint_id is TEXT, not a UUID foreign key to transparency_checkpoints(id).
-- Deliberately, for two reasons: an FK would make the insert FAIL for a row that
-- does not exist, which is exactly the case where an operator records a planted
-- id by hand after the fact; and an FK gives the quarantine row a cascade path
-- out of an append-only table. A plain UNIQUE text column can be written for an
-- id that may or may not exist, which is the only useful behaviour here.
--
-- REPLACING THE TRIPWIRE, NOT WEAKENING IT
-- =========================================
--
-- The quarantined_by / reason pair is the audit trail. The same three-layer
-- enforcement the other two tables carry is applied here, and it is applied in
-- the right ORDER: ENABLE ROW LEVEL SECURITY and the revokes come FIRST, before
-- the grants and the policies, so no statement in this file can ever execute
-- against a table with RLS off. (perf-hygiene's lesson: a hand-written migration
-- must set search_path before applying bare table names, or its GRANTs land on
-- the real public tables.) This file touches only public.* by name and does not
-- rewrite search_path, so it needs none.
--
-- Idempotent: every statement is guarded, so re-running is a no-op. Applied by
-- scripts/migrate.py, oldest first, like every other file in this directory.

DO $$
BEGIN
    IF to_regclass('public.transparency_quarantine') IS NULL THEN
        CREATE TABLE transparency_quarantine (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- Text, not a FK, on purpose: see the header. An operator quarantines
            -- a planted id that may not correspond to any row.
            checkpoint_id text NOT NULL UNIQUE,
            reason        text NOT NULL DEFAULT '',
            quarantined_by text,
            created_at    timestamptz NOT NULL DEFAULT now()
        );
        COMMENT ON TABLE transparency_quarantine IS
            'Checkpoints the signer must not build on. Append-only: quarantine is '
            'recorded, rows are never removed. See '
            'supabase/migrations/20261002240000_transparency_quarantine.sql.';
    END IF;
END
$$;

-- (a) Arm RLS and strip mutation privileges BEFORE anything is granted, so no
-- grant below can ever be live against an unprotected table. Same order the
-- F4 class-level fix established.
DO $$
DECLARE
    rol text;
BEGIN
    IF to_regclass('public.transparency_quarantine') IS NOT NULL THEN
        ALTER TABLE transparency_quarantine ENABLE ROW LEVEL SECURITY;
        REVOKE UPDATE, DELETE, TRUNCATE ON transparency_quarantine FROM PUBLIC;
        FOREACH rol IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = rol) THEN
                -- One REVOKE per role, and it happens here, BEFORE any grant in
                -- this file: the point is to be certain the table carries no
                -- privilege for a role it should not serve while RLS is being
                -- armed. service_role's SELECT is re-granted with a policy in
                -- step (d); anon and authenticated get nothing at all.
                EXECUTE format('REVOKE ALL ON transparency_quarantine FROM %I', rol);
            END IF;
        END LOOP;
    END IF;
END
$$;

-- (b) The append-only trigger, same function as the other two tables so there is
-- one definition of what append-only means in this schema.
DO $$
BEGIN
    -- to_regprocedure, not to_regclass: the guard is on a FUNCTION, and
    -- to_regclass('...mutation') on a function name returns NULL. That made this
    -- whole block a silent no-op, and the append-only trigger was never created
    -- -- the exact "looks applied, enforces nothing" shape this batch exists to
    -- close. Caught by asking Postgres whether the trigger existed rather than
    -- whether the migration raised.
    IF to_regclass('public.transparency_quarantine') IS NOT NULL
       AND to_regprocedure('transparency_reject_mutation()') IS NOT NULL THEN
        DROP TRIGGER IF EXISTS trg_transparency_quarantine_no_mutation
            ON transparency_quarantine;
        CREATE TRIGGER trg_transparency_quarantine_no_mutation
            BEFORE UPDATE OR DELETE OR TRUNCATE ON transparency_quarantine
            FOR EACH STATEMENT
            EXECUTE FUNCTION transparency_reject_mutation();
    END IF;
END
$$;

-- (c) Grants and the policies, keyed to the signer so nothing widens for anyone
-- else. Read-only: the signer consumes quarantine decisions, it does not make
-- them. Making them is an operator action, so the INSERT grant is deliberately
-- NOT given to the signer role -- otherwise an attacker who reached the signing
-- DSN could quarantine the genuine head and stall the signer at will. They
-- could not lower it (the head check still applies), but stalling is still a
-- denial of service worth not offering.
DO $$
BEGIN
    IF to_regclass('public.transparency_quarantine') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'transparency_signer') THEN
        GRANT SELECT ON transparency_quarantine TO transparency_signer;
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
            ON transparency_quarantine FROM transparency_signer;
        DROP POLICY IF EXISTS transparency_signer_read ON transparency_quarantine;
        CREATE POLICY transparency_signer_read ON transparency_quarantine
            FOR SELECT TO transparency_signer USING (true);
    END IF;
END
$$;

-- (d) The service_role key ships in the anon-facing trust path on some setups,
-- so it gets RLS-aware SELECT rather than a bare grant: a table with RLS on and
-- no policy reads as an EMPTY table, which is the most dangerous shape because
-- it looks like "nothing is quarantined" -- exactly the state a signer needs to
-- be wrong about.
DO $$
BEGIN
    IF to_regclass('public.transparency_quarantine') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        GRANT SELECT ON transparency_quarantine TO service_role;
        DROP POLICY IF EXISTS service_role_read ON transparency_quarantine;
        CREATE POLICY service_role_read ON transparency_quarantine
            FOR SELECT TO service_role USING (true);
    END IF;
END
$$;