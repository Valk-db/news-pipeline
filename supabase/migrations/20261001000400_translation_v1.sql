-- Translation columns on raw_articles (additive, nullable, no backfill needed).
-- Every article gets language-detected; non-English articles get English
-- headline/body. Originals are never modified; the UI reads
-- COALESCE(title_en, title). Apply via the Supabase dashboard SQL editor.

ALTER TABLE raw_articles
    ADD COLUMN IF NOT EXISTS detected_language VARCHAR(16),
    ADD COLUMN IF NOT EXISTS title_en TEXT,
    ADD COLUMN IF NOT EXISTS body_text_en TEXT;

CREATE INDEX IF NOT EXISTS ix_raw_articles_detected_language
    ON raw_articles (detected_language);
