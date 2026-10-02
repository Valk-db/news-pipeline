-- Dead Letters Migration
-- Items the pipeline could not process, kept instead of silently discarded.
-- Added on the procmon branch procmon/batch-trust. payload is JSON, not JSONB, because
-- that is what src/schema/models.py declares.

-- Two shapes in one table. article_id points at an article row that exists but never
-- reached a story, and payload carries the item itself for items that never became a row
-- at all, such as a feed entry that would not parse. article_id is cleared, not cascaded,
-- when the article goes away, because the failure outlives the article.
CREATE TABLE IF NOT EXISTS dead_letters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID,
    stage VARCHAR(50) NOT NULL,
    reason TEXT NOT NULL,
    article_id UUID REFERENCES raw_articles(id) ON DELETE SET NULL,
    payload JSON,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_dead_letters_run_id ON dead_letters(run_id);
CREATE INDEX IF NOT EXISTS ix_dead_letters_article_id ON dead_letters(article_id);
CREATE INDEX IF NOT EXISTS ix_dead_letters_stage ON dead_letters(stage);
CREATE INDEX IF NOT EXISTS ix_dead_letters_created_at ON dead_letters(created_at);

ALTER TABLE dead_letters ENABLE ROW LEVEL SECURITY;
