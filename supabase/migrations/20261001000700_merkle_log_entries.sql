-- Merkle log entries: the transparency log the RSS evidence locker stamps into.
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
-- Append only: there is no UPDATE or DELETE path, by design. The chain is contiguous
-- from index 0, and "index" is UNIQUE so a forked chain from two concurrent writers
-- fails loudly instead of silently.
--
-- Idempotent: safe to run more than once. src/ingestion/rss_evidence.py still probes
-- for the table and disables stamping when it is absent, which keeps that code correct
-- for a database that has not had this file applied yet.
--
-- No other schema change is needed. entity_edges (the GDELT radar join target)
-- and raw_articles both already exist.

CREATE TABLE IF NOT EXISTS merkle_log_entries (
    id                 UUID NOT NULL,
    index              INTEGER NOT NULL,          -- gapless; entry 0 is the first append
    hash_scheme        VARCHAR(32) NOT NULL,      -- "n1:sha256", stored so a v2 cannot be confused with v1
    timestamp          TIMESTAMPTZ NOT NULL,
    payload            JSON NOT NULL,             -- {"type": "rss_evidence", "url": ..., "fetched_at": ..., "body_sha256": ..., ...}
    canonical_payload  TEXT NOT NULL,             -- exact UTF-8 bytes that were hashed
    leaf_hash          VARCHAR(64) NOT NULL,      -- sha256 hex of canonical_payload
    chain_hash         VARCHAR(64) NOT NULL,      -- sha256 hex of prev chain_hash || leaf_hash
    PRIMARY KEY (id),
    UNIQUE (index)
);

CREATE INDEX IF NOT EXISTS ix_merkle_log_entries_timestamp
    ON merkle_log_entries (timestamp);

-- The log is signed evidence, not public content: close the anonymous REST path.
ALTER TABLE merkle_log_entries ENABLE ROW LEVEL SECURITY;