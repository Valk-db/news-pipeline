-- transparency_checkpoints + raw_articles.log_index: what the public proof
-- permalinks (/proof/{article_id}) read.
--
-- transparency_checkpoints generated from the SQLAlchemy model at
-- src/transparency/store.py (TransparencyBase -> TransparencyCheckpoint)
-- with the postgres dialect, so this file and the model cannot drift without
-- the generated text changing:
--
--   python -c "from sqlalchemy.schema import CreateTable, CreateIndex; \
--     from sqlalchemy.dialects import postgresql; \
--     from src.transparency.store import TransparencyCheckpoint; \
--     d = postgresql.dialect(); \
--     print(CreateTable(TransparencyCheckpoint.__table__).compile(dialect=d)); \
--     [print(CreateIndex(i).compile(dialect=d)) for i in TransparencyCheckpoint.__table__.indexes]"
--
-- Apply through the Supabase dashboard SQL editor, together with
-- docs/rss-evidence-merkle-ddl.sql if that has not been applied yet. Until
-- both tables exist, the proof page renders the honest "proof pending"
-- states: it never fabricates a proof.
--
-- raw_articles.log_index links an article row to its merkle_log_entries.index.
-- It is written by src/ingestion/rss_evidence.py stamp_observations() at
-- stamp time. NULL means the article was never stamped.
--
-- Append only for transparency_checkpoints: there is no UPDATE or DELETE
-- path, by design. A permalink anchors to the newest checkpoint covering its
-- entry, so readers verify against a published signed root, never the
-- operator's moving head.

CREATE TABLE IF NOT EXISTS transparency_checkpoints (
    id           UUID NOT NULL,
    tree_size    INTEGER NOT NULL,           -- entries covered: [0, tree_size)
    merkle_root  VARCHAR(64) NOT NULL,       -- hex of the root over those entries
    chain_hash   VARCHAR(64) NOT NULL,       -- hex of the last covered entry's chain hash
    timestamp    TIMESTAMP WITH TIME ZONE NOT NULL,  -- when the operator signed
    signature    TEXT NOT NULL,              -- hex of the detached signature
    algorithm    VARCHAR(64) NOT NULL,       -- e.g. "ed25519", "hmac-sha256-dev"
    key_id       VARCHAR(128) NOT NULL,
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_transparency_checkpoints_tree_size
    ON transparency_checkpoints (tree_size);

-- Link from an archived article to its evidence stamp. Additive and nullable;
-- existing rows stay NULL (unstamped) until the evidence locker stamps them.
ALTER TABLE raw_articles
    ADD COLUMN IF NOT EXISTS log_index INTEGER;
