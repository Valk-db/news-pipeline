-- Article revisions and corrections: the append-only record of a stealth edit.
--
-- raw_articles holds a single mutable snapshot of what a URL served when we first
-- ingested it, which is exactly what a stealth edit destroys. These two tables are the
-- record of every change observed afterwards: one row per observed change in
-- article_revisions, never an update to an older one, and one row per correction or
-- update notice seen inside a revision in article_corrections.
--
-- change_kind labels carry the SQLAlchemy type name and the Python member NAMES, matching
-- src/schema/models.py (Enum(ChangeKind, name="articlechangekind")).
--
-- Idempotent: safe to run more than once.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'articlechangekind' AND n.nspname = 'public') THEN
        CREATE TYPE articlechangekind AS ENUM ('ACKNOWLEDGED', 'STEALTH');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS article_revisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id UUID NOT NULL REFERENCES raw_articles(id) ON DELETE CASCADE,
    -- 1 is the first change observed after the ingested baseline; increments per article.
    revision_number INTEGER NOT NULL,
    -- NULL when this revision was the first change seen, i.e. it was diffed against the
    -- ingested body_text rather than against a stored revision.
    previous_revision_id UUID REFERENCES article_revisions(id) ON DELETE SET NULL,
    content_hash VARCHAR(64) NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,         -- when *we* re-fetched it
    displayed_at TIMESTAMPTZ,                -- timestamp the page itself displayed, if any
    change_kind articlechangekind NOT NULL DEFAULT 'STEALTH',
    changed_paragraphs INTEGER NOT NULL DEFAULT 0,
    diff_excerpt TEXT,                       -- capped unified diff against the previous revision
    diff_truncated BOOLEAN NOT NULL DEFAULT FALSE,
    correction_count INTEGER NOT NULL DEFAULT 0,
    -- merkle_log_entries.index when this revision was appended to the transparency log.
    log_index INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_article_revision_number UNIQUE (article_id, revision_number)
);

CREATE INDEX IF NOT EXISTS ix_article_revisions_article_id ON article_revisions (article_id);
CREATE INDEX IF NOT EXISTS ix_article_revisions_fetched_at ON article_revisions (fetched_at);
CREATE INDEX IF NOT EXISTS ix_article_revisions_content_hash ON article_revisions (content_hash);

-- A correction or update notice seen in a revision's text, as a first-class event. The
-- changed text such a notice refers to stays in the parent revision's diff_excerpt.
CREATE TABLE IF NOT EXISTS article_corrections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id UUID NOT NULL REFERENCES raw_articles(id) ON DELETE CASCADE,
    revision_id UUID NOT NULL REFERENCES article_revisions(id) ON DELETE CASCADE,
    signal VARCHAR(50) NOT NULL,            -- matched pattern label, e.g. correction, erratum
    location VARCHAR(20) NOT NULL,           -- top, body, corrections_block
    snippet TEXT NOT NULL,                   -- the matched text, capped
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_article_corrections_article_id ON article_corrections (article_id);
CREATE INDEX IF NOT EXISTS ix_article_corrections_revision_id ON article_corrections (revision_id);
CREATE INDEX IF NOT EXISTS ix_article_corrections_signal ON article_corrections (signal);

ALTER TABLE article_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE article_corrections ENABLE ROW LEVEL SECURITY;