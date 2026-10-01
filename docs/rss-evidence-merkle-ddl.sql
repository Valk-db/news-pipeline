-- merkle_log_entries: the transparency log the RSS evidence locker stamps into.
--
-- Generated from the SQLAlchemy model at src/transparency/log.py
-- (TransparencyBase -> MerkleLogEntry) with the postgres dialect, so this file
-- and the model cannot drift without the generated text changing:
--
--   python -c "from sqlalchemy.schema import CreateTable, CreateIndex; \
--     from sqlalchemy.dialects import postgresql; \
--     from src.transparency.log import MerkleLogEntry; \
--     d = postgresql.dialect(); \
--     print(CreateTable(MerkleLogEntry.__table__).compile(dialect=d)); \
--     [print(CreateIndex(i).compile(dialect=d)) for i in MerkleLogEntry.__table__.indexes]"
--
-- Apply through the Supabase dashboard SQL editor. The table does NOT exist in
-- the dev database yet, which is why src/ingestion/rss_evidence.py handles the
-- missing-table case defensively: stamping is disabled for the run, the article
-- rows still persist, and content_hash plus provenance stay on the result dict
-- so a later step can stamp retroactively once this DDL has been applied.
--
-- No other schema change is needed. entity_edges (the GDELT radar join target)
-- and raw_articles both already exist.
--
-- Append only: there is no UPDATE or DELETE path, by design. The chain is
-- contiguous from index 0, and `index` is UNIQUE so a forked chain from two
-- concurrent writers fails loudly instead of silently.

CREATE TABLE IF NOT EXISTS merkle_log_entries (
    id                 UUID NOT NULL,
    index              INTEGER NOT NULL,          -- gapless; entry 0 is the first append
    hash_scheme        VARCHAR(32) NOT NULL,      -- "n1:sha256", stored so a v2 cannot be confused with v1
    timestamp          TIMESTAMP WITH TIME ZONE NOT NULL,
    payload            JSON NOT NULL,            -- {"type": "rss_evidence", "url": ..., "fetched_at": ..., "body_sha256": ..., ...}
    canonical_payload  TEXT NOT NULL,             -- exact UTF-8 bytes that were hashed
    leaf_hash          VARCHAR(64) NOT NULL,     -- sha256 hex of canonical_payload
    chain_hash         VARCHAR(64) NOT NULL,     -- sha256 hex of prev chain_hash || leaf_hash
    PRIMARY KEY (id),
    UNIQUE (index)
);

CREATE INDEX IF NOT EXISTS ix_merkle_log_entries_timestamp
    ON merkle_log_entries (timestamp);