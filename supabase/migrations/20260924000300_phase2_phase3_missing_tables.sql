-- Phase 2 (Multimedia & Snippet Enrichment) and Phase 3 (Historical Reliability Rating)
-- schema. These tables existed only as SQLAlchemy models until now; nothing under
-- supabase/migrations/ ever created them, so any environment not bootstrapped by
-- init_db() (Base.metadata.create_all) -- including the deployed curation UI -- was
-- missing them entirely. Idempotent: safe to re-run.

-- Enum labels match the Python enum members' NAMES (uppercase), not their lowercase
-- .value strings: none of these Enum() columns declare values_callable, so SQLAlchemy
-- binds/queries using the member name by default -- same convention as the existing
-- sourcetier ('TIER4') and status ('EXPIRED') enums.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'mediatype') THEN
        CREATE TYPE mediatype AS ENUM ('IMAGE', 'VIDEO', 'AUDIO', 'EMBED');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'snippettype') THEN
        CREATE TYPE snippettype AS ENUM ('QUOTE', 'STAT', 'FACT', 'SUMMARY', 'CLAIM');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'verdict') THEN
        CREATE TYPE verdict AS ENUM ('TRUE', 'MOSTLY_TRUE', 'MIXED', 'MOSTLY_FALSE', 'FALSE', 'UNVERIFIED');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'factchecker') THEN
        CREATE TYPE factchecker AS ENUM ('CLAIMBUSTER', 'LLM_VERIFIER', 'CLAIMREVIEW', 'MANUAL');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'severity') THEN
        CREATE TYPE severity AS ENUM ('MINOR', 'MODERATE', 'MAJOR', 'RETRACTION');
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS media_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id UUID REFERENCES raw_articles(id) ON DELETE CASCADE,
    story_id UUID REFERENCES stories(id) ON DELETE CASCADE,
    media_type mediatype NOT NULL,
    url TEXT NOT NULL,
    thumbnail_url TEXT,
    alt_text TEXT,
    width INTEGER,
    height INTEGER,
    duration_seconds INTEGER,
    source VARCHAR(100),
    source_id VARCHAR(100),
    meta_data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_media_assets_article_id ON media_assets(article_id);
CREATE INDEX IF NOT EXISTS ix_media_assets_story_id ON media_assets(story_id);
CREATE INDEX IF NOT EXISTS ix_media_assets_type ON media_assets(media_type);

CREATE TABLE IF NOT EXISTS snippets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    article_id UUID NOT NULL REFERENCES raw_articles(id) ON DELETE CASCADE,
    snippet_type snippettype NOT NULL DEFAULT 'QUOTE',
    text TEXT NOT NULL,
    position INTEGER,
    entities JSONB,
    minhash_signature JSONB,
    confidence INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_snippets_story_id ON snippets(story_id);
CREATE INDEX IF NOT EXISTS ix_snippets_article_id ON snippets(article_id);
CREATE INDEX IF NOT EXISTS ix_snippets_type ON snippets(snippet_type);

CREATE TABLE IF NOT EXISTS source_reliability_snapshots (
    id SERIAL PRIMARY KEY,
    source_domain VARCHAR(255) NOT NULL,
    snapshot_date TIMESTAMPTZ NOT NULL,
    factual_accuracy INTEGER,
    correction_rate INTEGER,
    consensus_alignment INTEGER,
    transparency_score INTEGER,
    reliability_score INTEGER NOT NULL,
    total_claims_verified INTEGER NOT NULL DEFAULT 0,
    claims_true INTEGER NOT NULL DEFAULT 0,
    claims_false INTEGER NOT NULL DEFAULT 0,
    claims_mixed INTEGER NOT NULL DEFAULT 0,
    corrections_count INTEGER NOT NULL DEFAULT 0,
    articles_sampled INTEGER NOT NULL DEFAULT 0,
    tier_at_snapshot VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_source_date UNIQUE (source_domain, snapshot_date)
);
CREATE INDEX IF NOT EXISTS ix_source_reliability_source_date ON source_reliability_snapshots(source_domain, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_source_reliability_date ON source_reliability_snapshots(snapshot_date);

CREATE TABLE IF NOT EXISTS fact_check_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_domain VARCHAR(255) NOT NULL,
    article_id UUID REFERENCES raw_articles(id) ON DELETE SET NULL,
    claim TEXT NOT NULL,
    claim_hash VARCHAR(64) NOT NULL,
    verdict verdict NOT NULL,
    confidence INTEGER NOT NULL DEFAULT 50,
    fact_checker factchecker NOT NULL,
    fact_checker_url TEXT,
    explanation TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claim_date TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_fact_check_source_date ON fact_check_records(source_domain, checked_at);
CREATE INDEX IF NOT EXISTS ix_fact_check_verdict ON fact_check_records(verdict);
CREATE INDEX IF NOT EXISTS ix_fact_check_claim_hash ON fact_check_records(claim_hash);

CREATE TABLE IF NOT EXISTS correction_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_domain VARCHAR(255) NOT NULL,
    article_id UUID REFERENCES raw_articles(id) ON DELETE SET NULL,
    original_text TEXT NOT NULL,
    corrected_text TEXT NOT NULL,
    correction_summary TEXT,
    severity severity NOT NULL DEFAULT 'MODERATE',
    correction_date TIMESTAMPTZ NOT NULL,
    correction_url TEXT,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_correction_source_date ON correction_records(source_domain, correction_date);
CREATE INDEX IF NOT EXISTS ix_correction_article ON correction_records(article_id);
CREATE INDEX IF NOT EXISTS ix_correction_severity ON correction_records(severity);

CREATE TABLE IF NOT EXISTS article_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id UUID NOT NULL REFERENCES raw_articles(id) ON DELETE CASCADE,
    model VARCHAR(100) NOT NULL,
    embedding JSONB NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_article_embeddings_article_id ON article_embeddings(article_id);
CREATE INDEX IF NOT EXISTS ix_article_embeddings_model ON article_embeddings(model);

CREATE TABLE IF NOT EXISTS story_embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    model VARCHAR(100) NOT NULL,
    embedding JSONB NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_story_embeddings_story_id ON story_embeddings(story_id);
CREATE INDEX IF NOT EXISTS ix_story_embeddings_model ON story_embeddings(model);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id SERIAL PRIMARY KEY,
    canonical_entity_id UUID NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    alias VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_canonical_alias UNIQUE (canonical_entity_id, alias)
);
CREATE INDEX IF NOT EXISTS ix_entity_aliases_alias ON entity_aliases(alias);

-- Close the anonymous REST API path on every new table, matching scripts/enable_rls.py's
-- policy for all other tables (service-role backend bypasses RLS; no policies = no anon access).
ALTER TABLE media_assets ENABLE ROW LEVEL SECURITY;
ALTER TABLE snippets ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_reliability_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_check_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE correction_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE article_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE story_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_aliases ENABLE ROW LEVEL SECURITY;