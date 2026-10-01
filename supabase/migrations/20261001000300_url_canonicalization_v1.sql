-- URL canonicalization scheme u1, additive only.
--
-- raw_articles.url and raw_articles.url_hash are the columns the signed Merkle
-- log and every dedup set already refer to, so this migration adds new columns
-- beside them and rewrites nothing. The backfill in
-- scripts/backfill_url_hash_v1.py fills them, and url_aliases records how the
-- old hash maps onto the new one so a lookup can be translated either way.
--
-- url_hash_v1 is deliberately NOT unique yet. The backfill is expected to find
-- rows that collapse under u1, and the ingest workstream has to decide what to
-- do with those before a unique constraint can be added.

ALTER TABLE raw_articles
    ADD COLUMN IF NOT EXISTS canonical_url_v1 TEXT,
    ADD COLUMN IF NOT EXISTS url_hash_v1 VARCHAR(64);

-- A non unique index, because a hash collision here means two real articles
-- merged under u1, which is a finding rather than an error.
CREATE INDEX IF NOT EXISTS ix_raw_articles_url_hash_v1
    ON raw_articles(url_hash_v1);

-- One row per legacy url_hash, so an old hash can still be resolved to the u1
-- identity after the fact. old_url_hash is the primary key because exactly one
-- u1 identity can own a legacy hash, which is what makes a second pass of the
-- backfill safe.
CREATE TABLE IF NOT EXISTS url_aliases (
    old_url_hash VARCHAR(64) PRIMARY KEY,
    url_hash_v1 VARCHAR(64) NOT NULL,
    canonical_url_v1 TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_url_aliases_url_hash_v1
    ON url_aliases(url_hash_v1);
