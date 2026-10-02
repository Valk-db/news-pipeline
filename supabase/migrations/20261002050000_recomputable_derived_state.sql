-- Make every derived row record which analyzer produced it and what it was computed from.
--
-- raw_articles is the immutable raw layer. Everything downstream of it -- reporting units,
-- stories, entities, embeddings, claims, reliability snapshots -- is derived, and until now a
-- derived row carried no record of the analyzer that produced it or of the input it was
-- computed from. The only remedy for a bug in dedupe or clustering was a full re-ingest,
-- because nothing could say which existing rows the bug affected.
--
-- Two columns per table answer the two questions that makes recomputation possible:
--
--   analyzer_version  which analyzer produced this row (src/shared/analyzer_versions.py owns
--                     the constants). Bump the constant when that analyzer's logic changes and
--                     the affected rows become identifiable instead of indistinguishable.
--   input_hash        SHA256 over the canonical input the row was computed from. Two runs over
--                     the same input agree, so a rebuild is verifiable rather than merely
--                     repeatable.
--
-- Both are nullable, and that is the point rather than a concession: every row that already
-- exists has NULL in both. NULL therefore means "written before this was recorded", which for
-- scripts/recompute_derived.py is stale -- it cannot be shown to have come from the current
-- analyzer. Rows that exist today are all in that state, so the first recompute of any stage
-- is a full recompute of it. The audit mode of the script reports that count before deleting
-- anything.
--
-- The columns are TEXT rather than an enum on purpose: a new version is a new string, and an
-- enum type would mean an ALTER TYPE for every bump. NOT VALID constraints are not used
-- either -- there is nothing to validate, since both values are free-form by contract.
--
-- Length: both are String(64) in src/schema/models.py, not unbounded TEXT, so the width is
-- self-documenting (a SHA256 is 64 hex chars) and matches the four existing hash columns in
-- this schema (raw_articles.url_hash, raw_articles.content_hash,
-- raw_articles.url_hash_v1, fact_check_records.claim_hash). Every value written today is well
-- under the limit.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, and a table that does not exist yet is skipped so the
-- later CREATE TABLE migration can still make it. Re-applying is a no-op.
--
-- No index. The recompute script's selection predicate (analyzer_version IS DISTINCT FROM the
-- current constant) is a full scan, and that is the right plan at this size: every derived
-- table in dev holds thousands of rows at most, and an index that exists only to make a
-- deliberate, occasional rebuild fast would be a write cost on every pipeline run forever.
-- Revisit if a table passes the point where the rebuild's scan is visible next to the run.
--
-- Deliberately NOT touched: raw_articles. It is the immutable source, and it has no analyzer
-- to record. Its pipeline-managed pointer columns (reporting_unit_id, terminal_state) are
-- written by the stages, not by this migration.
--
-- Same shape and reason as 20261002030000_missing_id_defaults.sql: one DO block, the table
-- list as data, every table skipped if it is not there yet.

DO $$
DECLARE
    tbl text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'reporting_units',
        'stories',
        'story_unit_links',
        'canonical_entities',
        'entity_aliases',
        'event_geometries',
        'events',
        'article_embeddings',
        'story_embeddings',
        'claims',
        'claim_evidence',
        'entity_edges',
        'story_topic_groups',
        'source_reliability_snapshots'
    ]
    LOOP
        IF to_regclass('public.' || tbl) IS NULL THEN
            CONTINUE;  -- table not created yet; the earlier schema migration will carry it
        END IF;

        EXECUTE format(
            'ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS analyzer_version text',
            tbl
        );
        EXECUTE format(
            'ALTER TABLE public.%I ADD COLUMN IF NOT EXISTS input_hash varchar(64)',
            tbl
        );
    END LOOP;
END $$;