-- S-P1-2: constrain raw_articles.log_index (UNIQUE + foreign key).
--
-- The proof permalink joined the article to its log entry through the bare
-- raw_articles.log_index column: no UNIQUE, no foreign key, and the leaf
-- payload did not name the article. Repointing log_index served article B's
-- evidence under article A's title/URL while the page still said "Inclusion
-- verified".
--
-- This migration constrains the column:
--
--   UNIQUE (log_index)                      one article owns each log entry
--   FOREIGN KEY (log_index)
--     REFERENCES merkle_log_entries(index)  the link can only point at an
--                                           entry that exists
--
-- The cryptographic half of the fix is in the leaf payload itself:
-- src/ingestion/rss_evidence.py stamp_observations() now stamps
-- {"article_id": <str(article.id)>, ...} inside the hashed payload, and
-- curation_ui/proofs.py verifies the binding (payload article_id plus a
-- url/title/body_sha256 diff against the article row) on every proof view.
--
-- Idempotent: safe to run more than once. Constraint validation fails loudly
-- on a database whose existing rows violate the constraint (duplicate or
-- orphaned log_index values) instead of silently laundering them; fix the
-- data, then re-run.
--
-- Requires 20261001000700_merkle_log_entries.sql (the referenced table) and
-- 20261002000100_proof_permalinks.sql (the column); scripts/migrate.py
-- applies oldest first, so both predate this file.

DO $$
BEGIN
    IF to_regclass('public.raw_articles') IS NOT NULL
       AND to_regclass('public.merkle_log_entries') IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_raw_articles_log_index') THEN
            ALTER TABLE raw_articles
                ADD CONSTRAINT uq_raw_articles_log_index UNIQUE (log_index);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_raw_articles_log_index') THEN
            ALTER TABLE raw_articles
                ADD CONSTRAINT fk_raw_articles_log_index
                FOREIGN KEY (log_index) REFERENCES merkle_log_entries(index);
        END IF;
    END IF;
END
$$;
