-- pgvector readiness: the extension, and nothing else.
--
-- Backlog item 13's first step is exactly one statement. Embeddings, an HNSW
-- index and a match_articles RPC are deliberately NOT here: they are later work
-- that should be measured against a real nearest-duplicate eval set before any
-- of it lands, and an embedding column added today would be a nullable column
-- with no writer behind it.
--
-- IF NOT EXISTS makes this a no-op on every re-run, which is what
-- scripts/migrate.py needs: it applies every file in this directory every time.
-- Verified on dev: pgvector 0.8.2 was available but not installed before this
-- file, and installed after.

CREATE EXTENSION IF NOT EXISTS vector;