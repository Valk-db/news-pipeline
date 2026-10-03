-- transparency signer v3: the tree_scheme column the F9 finding requires, the
-- entry-timestamp commitment F15 signs over, and the idle-transaction timeout
-- on the signer role from F14.
--
-- WHY THIS FILE EXISTS
-- ====================
--
-- The F9/F15 work landed in src/transparency/checkpoint.py, signing.py,
-- proofs.py and store.py on this branch, and the model in store.py has carried
-- two columns that NO migration ever created:
--
--   tree_scheme       NOT NULL, default 'n1-ct-dup-v1'
--   entry_timestamps  nullable text, hex of the entry-timestamp root
--
-- A model column with no migration is dead on arrival, and no SQLite test will
-- say so: pytest builds its schema from the SQLAlchemy metadata, so the ORM
-- column always exists in the test database whether or not Postgres has it.
-- The failure appears only on the real database, as
-- UndefinedColumn: column transparency_checkpoints.tree_scheme does not exist,
-- raised inside the signer -- which means the signing cron 500s on every run.
-- This file is that migration. It is the only place the DDL for these two
-- columns exists.
--
-- WHY tree_scheme IS NOT NULL, AND WHY IT IS BACKFILLED RATHER THAN NULLABLE
-- =========================================================================
--
-- A root is a number that only means something next to the tree hashing that
-- produced it. The legacy root (TREE_SCHEME_CT_V1, "n1-ct-dup-v1") and the
-- RFC-6962 root are different numbers over the same leaves, by construction --
-- that is exactly why F9 exists: the legacy construction has a second-preimage
-- shape where [a,b,c] and [a,b,c,c] collide, and the RFC-6962 construction with
-- its 0x00/0x01 leaf/internal domain separation does not.
--
-- So "is this root the root of this log" is unanswerable unless the scheme
-- travels with it. checkpoint_format was being used to carry that, and it is
-- lossy in the wrong direction: it conflates "what shape the signed note is"
-- with "how the tree was hashed", and the signer needs to compare a stored root
-- against a re-derivation without first parsing the note.
--
-- The column is NOT NULL rather than nullable so that two states stay
-- distinguishable:
--
--   - NULL      a row from a database where THIS migration has not run. The
--               signer must not mistake it for a scheme it can compare against.
--   - a scheme  a claim about how that row's root was computed.
--
-- A nullable column collapses those, and a signer that guessed "unknown means
-- legacy" would compare a v3 RFC-6962 root against a legacy re-derivation and
-- refuse forever -- or, worse, the reverse.
--
-- The backfill is derived from checkpoint_format, not invented. The scheme is a
-- pure function of the format:
--
--   n1-json-v1                 -> n1-ct-dup-v1     (pre-2026-10-02)
--   c2sp-tlog-checkpoint-v2    -> n1-ct-dup-v1
--   c2sp-tlog-checkpoint-v3    -> rfc6962-sha256
--
-- which is src/transparency/checkpoint.py TREE_SCHEME_FOR_FORMAT, written out
-- here as a CASE so the database states the mapping rather than importing it.
-- A row whose format is NULL is v1 by definition -- that is what
-- Checkpoint.from_dict defaults to, and it is the same assumption
-- 20261002200000 made when it backfilled checkpoint_format. Rows in a format
-- this file does not name fall back to the legacy scheme, which is the
-- conservative direction: it claims less about the row than the alternative
-- would, so a row is never re-labelled as a stronger scheme than it is.
--
-- As in 20261002200000, the backfill is the one statement here that mutates an
-- existing transparency row, and the append-only trigger from 20261002193000
-- forbids exactly that. So the trigger is stood down for the backfill only and
-- re-armed immediately, inside a subtransaction: if the UPDATE raises, the
-- DISABLE rolls back with it and the table is never left mutable.
--
-- WHY NO NEW UNIQUE INDEX (this is the F11 question, answered)
-- ===========================================================
--
-- F11 asked whether the uniqueness guarantee on this table is UNIQUE(tree_size)
-- or UNIQUE(tree_size, format). It is the second, from
-- 20261002200000_transparency_signer_v2.sql:
--
--   CREATE UNIQUE INDEX uq_transparency_checkpoints_size_format
--       ON transparency_checkpoints
--          (tree_size, coalesce(checkpoint_format, 'n1-json-v1'));
--
-- UNIQUE(tree_size) alone would be wrong and was dropped on purpose there: v1
-- and v2 rows are both append-only and cannot be rewritten, so a format change
-- necessarily republishes at a size that already has a row, and a UNIQUE on
-- tree_size would make the upgrade impossible to complete.
--
-- Does F9 need a third column in that index? No, and the reason is worth
-- recording so nobody "fixes" it later. The scheme is a pure function of the
-- format, so (tree_size, format) already determines (tree_size, scheme). The
-- existing index therefore already forbids two rows with different roots under
-- the same scheme at the same tree_size, which is the property that matters.
-- Adding tree_scheme to the index would change nothing an attacker could
-- exploit. It is left alone.
--
-- entry_timestamps
-- ================
--
-- The hex of the v3 entry-timestamp commitment: a root over the (index,
-- timestamp) pairs the checkpoint covers. F15's finding was that entry
-- timestamps reached the signed note as unsigned context, so a database writer
-- could restamp the whole log and every entry-time claim a reader had would
-- change while every root stayed valid. Folding the commitment into the signed
-- bytes is what makes entry times part of what the signature covers.
--
-- Nullable because v1 and v2 rows have none -- they were signed without it and
-- must keep verifying byte-for-byte. It MUST be stored rather than recomputed
-- on load: it is inside the signed bytes, so a v3 row that lost it on reload
-- would fail its own signature verification on the next run. store.py
-- save_checkpoint() writes it and to_signed() reads it back.
--
-- F14: idle_in_transaction_session_timeout ON THE SIGNER ROLE
-- =========================================================
--
-- The signer's failure mode that matters most is not a wrong answer, it is a
-- run that never finishes. sign_next_checkpoint() opens a transaction and holds
-- it for the whole run: it takes pg_try_advisory_xact_lock, reads the log,
-- verifies the previous checkpoint, signs, verifies its own signature, then
-- inserts. Any of those steps can block indefinitely -- a lock wait, a slow
-- read. The default idle_in_transaction_session_timeout is 0, i.e. unlimited,
-- so a blocked signer session pins its snapshot and its advisory xact lock
-- indefinitely, and every subsequent cron fire fails at the lock with
-- lock_held. One hang becomes permanent unavailability.
--
-- 30s is chosen to sit well above the longest healthy run (the whole log is
-- read and hashed in memory; on dev that is 14 entries) and well below the
-- point at which an operator would notice. It only fires on a session that is
-- IDLE inside a transaction, so it cannot kill a healthy run that is actively
-- working; it kills the one that is stuck.
--
-- This is ALTER ROLE, a NEW statement in a NEW file. The already-applied
-- 20261002230000_transparency_signer_rbac.sql is not edited: migrations that
-- have run are history, and editing one makes a fresh database and an existing
-- one disagree.
--
-- The second half of F14 is documentation, not DDL. pg_locks is readable by
-- PUBLIC, so the advisory lock that guards signing is publicly observable: the
-- lock's holder PID and the query it is running are visible to anyone who can
-- open a connection. That carries NO confidentiality worth protecting -- the
-- only thing it reveals is that a signing run is in progress, which the
-- existence of a fresh row in transparency_checkpoints reveals anyway, one
-- commit later. The lock's value is that it serialises signers, which is an
-- integrity property and does not depend on being secret. Making pg_locks
-- private would require revoking a grant Postgres needs for its own
-- introspection, for no security gain. Recorded in code at
-- signing._try_advisory_lock() and in the batch report instead.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, a guarded backfill that only touches
-- rows still NULL, and ALTER ROLE ... SET which is itself idempotent. Applied
-- by scripts/migrate.py, oldest first, like every other file in this directory.

-- ---------------------------------------------------------------- v3 columns
DO $$
BEGIN
    IF to_regclass('public.transparency_checkpoints') IS NULL THEN
        RETURN;
    END IF;
    ALTER TABLE transparency_checkpoints
        ADD COLUMN IF NOT EXISTS tree_scheme text,
        ADD COLUMN IF NOT EXISTS entry_timestamps text;

    BEGIN
        ALTER TABLE transparency_checkpoints
            DISABLE TRIGGER trg_transparency_checkpoints_no_mutation;
        UPDATE transparency_checkpoints
        SET tree_scheme = CASE
                -- A NULL or unrecognised format is a v1 row, which is the
                -- legacy scheme. Naming the legacy scheme is the conservative
                -- direction: it claims less about the row than rfc6962 would.
                WHEN checkpoint_format = 'c2sp-tlog-checkpoint-v3' THEN 'rfc6962-sha256'
                ELSE 'n1-ct-dup-v1'
            END
        WHERE tree_scheme IS NULL;
    EXCEPTION WHEN insufficient_privilege THEN
        -- Not the table owner. The columns are still added and the constraint
        -- below is still attempted; the backfill is left for a human to run
        -- deliberately. Same handling as 20261002200000.
        RAISE WARNING 'transparency_checkpoints: cannot disable append-only '
            'trigger, skipping tree_scheme backfill (%)', SQLERRM;
    END;
    ALTER TABLE transparency_checkpoints
        ENABLE TRIGGER trg_transparency_checkpoints_no_mutation;

    -- NOT NULL now that every existing row is labelled. The DEFAULT is the
    -- legacy scheme so a row inserted by anything that does not know about v3
    -- is labelled as what it almost certainly is, rather than being rejected or
    -- silently claimed as RFC-6962. The signer reads the column it stores
    -- (store.save_checkpoint sets it from checkpoint.tree_scheme()), so the
    -- default is a backstop for hand-inserted rows, not the normal path.
    ALTER TABLE transparency_checkpoints
        ALTER COLUMN tree_scheme SET DEFAULT 'n1-ct-dup-v1';
    ALTER TABLE transparency_checkpoints
        ALTER COLUMN tree_scheme SET NOT NULL;

    COMMENT ON COLUMN transparency_checkpoints.tree_scheme IS
        'Tree hashing that produced merkle_root: n1-ct-dup-v1 (legacy, collision-prone) or rfc6962-sha256. NOT NULL: a root is meaningless without it.';
    COMMENT ON COLUMN transparency_checkpoints.entry_timestamps IS
        'Hex root over the covered (index, timestamp) pairs; inside the v3 signed bytes. Null on v1/v2 rows, which were signed without it.';
END
$$;

-- ------------------------------------------------------- F14, signer role only
-- Guarded on the role existing, because a database that predates the RBAC
-- migration has no transparency_signer and must not fail this file. The ALTER
-- ROLE is deliberately NOT inside the DO block above: role GUC changes are
-- cluster-level, not table-scoped, and burying one in a guard that returns
-- early on a missing table would make the timeout's presence depend on an
-- unrelated table.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'transparency_signer') THEN
        RAISE WARNING 'transparency_signer role does not exist; skipping '
            'idle_in_transaction_session_timeout';
        RETURN;
    END IF;
    EXECUTE 'ALTER ROLE transparency_signer '
        'SET idle_in_transaction_session_timeout = 30000';
END
$$;