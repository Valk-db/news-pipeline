-- Phase 1 (viewpoint aggregation) schema changes. Idempotent: safe to re-run.
-- Migration A: Add TIER4 enum value (must be in its own transaction)
ALTER TYPE sourcetier ADD VALUE IF NOT EXISTS 'TIER4';-- Phase 1 (viewpoint aggregation) schema changes. Idempotent: safe to re-run.
-- Migration B: Add columns and index (runs after enum is committed)
ALTER TABLE stories ADD COLUMN IF NOT EXISTS tier3_unit_count integer NOT NULL DEFAULT 0;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS tier4_unit_count integer NOT NULL DEFAULT 0;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS viewpoint_cluster_id uuid;
CREATE INDEX IF NOT EXISTS ix_stories_viewpoint_cluster ON stories (viewpoint_cluster_id);-- Adds the EXPIRED label used by src/verification/cleanup.py. Idempotent.
-- Run this statement by itself in the Supabase SQL editor (a new enum label cannot be used in
-- the same transaction that adds it).
ALTER TYPE status ADD VALUE IF NOT EXISTS 'EXPIRED';-- Phase 2 (Multimedia & Snippet Enrichment) and Phase 3 (Historical Reliability Rating)
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
ALTER TABLE entity_aliases ENABLE ROW LEVEL SECURITY;-- Globe Events Schema Migration
-- Idempotent migration for event visualization tables and enums

-- Create enum types if they don't exist
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'event_type') THEN
        CREATE TYPE event_type AS ENUM (
            'conflict', 'protest', 'election', 'disaster', 'accident',
            'political', 'economic', 'health', 'environmental', 'crime',
            'sports', 'cultural', 'scientific', 'other'
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'event_geometry_type') THEN
        CREATE TYPE event_geometry_type AS ENUM (
            'point', 'polygon', 'linestring', 'multipoint',
            'multipolygon', 'multilinestring', 'geometrycollection'
        );
    END IF;
END $$;

-- Create event_geometries table
CREATE TABLE IF NOT EXISTS event_geometries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL,
    geometry_type event_geometry_type NOT NULL,
    geojson JSONB NOT NULL,
    properties JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Create event_layers table
CREATE TABLE IF NOT EXISTS event_layers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    description TEXT,
    filter_criteria JSONB NOT NULL DEFAULT '{}',
    style JSONB NOT NULL DEFAULT '{}',
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    is_visible BOOLEAN NOT NULL DEFAULT TRUE,
    min_zoom INTEGER NOT NULL DEFAULT 0,
    max_zoom INTEGER NOT NULL DEFAULT 20,
    color VARCHAR(7) NOT NULL DEFAULT '#3b82f6',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Create events table
CREATE TABLE IF NOT EXISTS events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    latitude VARCHAR(50) NOT NULL,
    longitude VARCHAR(50) NOT NULL,
    location_name VARCHAR(255),
    location_type VARCHAR(50),
    radius_km VARCHAR(50),
    start_time TIMESTAMPTZ NOT NULL,
    event_type event_type NOT NULL DEFAULT 'other',
    confidence VARCHAR(50) NOT NULL DEFAULT '0.5',
    source_count INTEGER NOT NULL DEFAULT 0,
    tier1_source_count INTEGER NOT NULL DEFAULT 0,
    entities JSONB,
    geometry_id UUID REFERENCES event_geometries(id) ON DELETE SET NULL,
    layer_id UUID REFERENCES event_layers(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ensure columns exist if table already existed (idempotent)
ALTER TABLE events
    ADD COLUMN IF NOT EXISTS layer_id UUID REFERENCES event_layers(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS geometry_id UUID REFERENCES event_geometries(id) ON DELETE SET NULL;

-- Add FK columns to canonical_entities if they don't exist
ALTER TABLE canonical_entities
    ADD COLUMN IF NOT EXISTS geometry_id UUID REFERENCES event_geometries(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS layer_id UUID REFERENCES event_layers(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS latitude VARCHAR(50),
    ADD COLUMN IF NOT EXISTS longitude VARCHAR(50),
    ADD COLUMN IF NOT EXISTS location_type VARCHAR(50),
    ADD COLUMN IF NOT EXISTS geonames_id VARCHAR(50),
    ADD COLUMN IF NOT EXISTS geojson JSONB;

-- Create indexes
CREATE INDEX IF NOT EXISTS ix_event_geometries_event_id ON event_geometries(event_id);
CREATE INDEX IF NOT EXISTS ix_event_layers_name ON event_layers(name);
CREATE INDEX IF NOT EXISTS ix_events_story_id ON events(story_id);
CREATE INDEX IF NOT EXISTS ix_events_layer_id ON events(layer_id);
CREATE INDEX IF NOT EXISTS ix_events_event_type ON events(event_type);
CREATE INDEX IF NOT EXISTS ix_events_location ON events(latitude, longitude);
CREATE INDEX IF NOT EXISTS ix_events_start_time ON events(start_time);
CREATE INDEX IF NOT EXISTS ix_canonical_entities_geometry_id ON canonical_entities(geometry_id);
CREATE INDEX IF NOT EXISTS ix_canonical_entities_layer_id ON canonical_entities(layer_id);

-- Seed default event layer
INSERT INTO event_layers (id, name, description, filter_criteria, style, is_default, is_visible, min_zoom, max_zoom, color)
VALUES (
    gen_random_uuid(),
    'default',
    'Default event layer',
    '{}',
    '{"color": "#3b82f6", "radius": 10000}',
    TRUE,
    TRUE,
    0,
    20,
    '#3b82f6'
)
ON CONFLICT DO NOTHING;-- Phase 2: claims, claim evidence, entity edges, topic groups
-- Idempotent migration -- see GRAND_PLAN.md §2

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'claim_type') THEN
        CREATE TYPE claim_type AS ENUM ('fact', 'allegation', 'prediction', 'quote');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'claim_stance') THEN
        CREATE TYPE claim_stance AS ENUM ('supports', 'disputes', 'neutral');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'edge_predicate') THEN
        CREATE TYPE edge_predicate AS ENUM (
            'corroborates', 'disputes', 'same_event_as', 'part_of_narrative', 'caused_by'
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    claim_type claim_type NOT NULL DEFAULT 'fact',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    id SERIAL PRIMARY KEY,
    claim_id UUID NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    unit_id UUID NOT NULL REFERENCES reporting_units(id) ON DELETE CASCADE,
    stance claim_stance NOT NULL DEFAULT 'neutral',
    confidence INTEGER NOT NULL DEFAULT 50,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_claim_unit UNIQUE (claim_id, unit_id)
);

CREATE TABLE IF NOT EXISTS entity_edges (
    id SERIAL PRIMARY KEY,
    subject_type VARCHAR(20) NOT NULL,
    subject_id UUID NOT NULL,
    predicate edge_predicate NOT NULL,
    object_type VARCHAR(20) NOT NULL,
    object_id UUID NOT NULL,
    confidence INTEGER NOT NULL DEFAULT 50,
    source_unit_id UUID REFERENCES reporting_units(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS topic_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    parent_group_id UUID REFERENCES topic_groups(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_topic_group_name_parent UNIQUE (name, parent_group_id)
);

CREATE TABLE IF NOT EXISTS story_topic_groups (
    id SERIAL PRIMARY KEY,
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    topic_group_id UUID NOT NULL REFERENCES topic_groups(id) ON DELETE CASCADE,
    confidence INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_story_topic_group UNIQUE (story_id, topic_group_id)
);

CREATE INDEX IF NOT EXISTS ix_claims_story_id ON claims(story_id);
CREATE INDEX IF NOT EXISTS ix_claims_claim_type ON claims(claim_type);
CREATE INDEX IF NOT EXISTS ix_claim_evidence_unit_id ON claim_evidence(unit_id);
CREATE INDEX IF NOT EXISTS ix_entity_edges_subject ON entity_edges(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS ix_entity_edges_object ON entity_edges(object_type, object_id);
CREATE INDEX IF NOT EXISTS ix_entity_edges_predicate ON entity_edges(predicate);
CREATE INDEX IF NOT EXISTS ix_topic_groups_parent ON topic_groups(parent_group_id);-- Source Topic Reliability Schema Migration
-- Per-(source_domain, topic_group) reliability score
-- See AGENT_TASKS.md v20 P3-A

CREATE TABLE IF NOT EXISTS source_topic_reliability (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_domain VARCHAR(255) NOT NULL,
    topic_group_id UUID NOT NULL REFERENCES topic_groups(id) ON DELETE CASCADE,
    score INTEGER NOT NULL,          -- 0-100, same convention as source_reliability_snapshots
    sample_size INTEGER NOT NULL DEFAULT 0,
    snapshot_date TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_domain, topic_group_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS ix_str_lookup ON source_topic_reliability(source_domain, topic_group_id);
CREATE INDEX IF NOT EXISTS ix_str_date ON source_topic_reliability(snapshot_date);