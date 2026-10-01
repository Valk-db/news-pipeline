-- Discovery search index: pg_trgm for fuzzy topic search on story headlines.
--
-- Apply via the Supabase dashboard SQL editor (dashboard-only step).
-- Idempotent: safe to run more than once.
--
-- What it does:
--   1. Enables the pg_trgm extension (needed for similarity() ranking).
--   2. Adds GIN trigram indexes on raw_articles.title and raw_articles.title_en
--      so topic search (e.g. "earthquake", "Macron") ranks by similarity
--      instead of falling back to slow ILIKE scans.
--
-- The app auto-detects pg_trgm at startup (_trigram_available probe) and uses
-- ILIKE matching when the extension is absent, so the UI works either way;
-- these indexes just make search fast and typo-tolerant.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_raw_articles_title_trgm
    ON raw_articles USING gin (title gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_raw_articles_title_en_trgm
    ON raw_articles USING gin (title_en gin_trgm_ops);
