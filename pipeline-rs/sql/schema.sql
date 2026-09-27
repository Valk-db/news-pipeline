-- Complete database schema for news-pipeline Rust port
-- This combines all Supabase migrations and adds missing base tables

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Enum types matching Rust models
CREATE TYPE source_tier AS ENUM ('tier1', 'tier2', 'tier3', 'tier4');
CREATE TYPE story_status AS ENUM ('pending', 'queued', 'posted', 'rejected', 'blocked', 'expired');
CREATE TYPE curated_post_status AS ENUM ('draft', 'approved', 'posted', 'failed');
CREATE TYPE event_type AS ENUM ('conflict', 'protest', 'election', 'disaster', 'accident', 'political', 'economic', 'health', 'environmental', 'crime', 'sports', 'cultural', 'scientific', 'other');
CREATE TYPE geometry_type AS ENUM ('point', 'polygon', 'linestring', 'multipoint', 'multipolygon', 'multilinestring', 'geometrycollection');
CREATE TYPE snippet_type AS ENUM ('quote', 'stat', 'fact', 'summary', 'claim');
CREATE TYPE media_type AS ENUM ('image', 'video', 'audio', 'embed');
CREATE TYPE fact_check_verdict AS ENUM ('true', 'mostly_true', 'mixed', 'mostly_false', 'false', 'unverified');
CREATE TYPE fact_checker AS ENUM ('claimbuster', 'llm_verifier', 'claimreview', 'manual');
CREATE TYPE correction_severity AS ENUM ('minor', 'moderate', 'major', 'retraction');
CREATE TYPE claim_type AS ENUM ('fact', 'allegation', 'prediction', 'quote');
CREATE TYPE claim_stance AS ENUM ('supports', 'disputes', 'neutral');
CREATE TYPE edge_predicate AS ENUM ('corroborates', 'disputes', 'same_event_as', 'part_of_narrative', 'caused_by');

-- Core tables

CREATE TABLE IF NOT EXISTS raw_articles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url TEXT NOT NULL,
    url_hash CHAR(64) NOT NULL UNIQUE,
    title TEXT NOT NULL,
    body_text TEXT,
    summary TEXT,
    source_domain VARCHAR(255) NOT NULL,
    source_tier source_tier NOT NULL DEFAULT 'tier3',
    published_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    entities JSONB,
    minhash_signature JSONB,
    content_hash CHAR(64),
    reporting_unit_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_raw_articles_published_at ON raw_articles(published_at);
CREATE INDEX IF NOT EXISTS ix_raw_articles_source_domain ON raw_articles(source_domain);
CREATE INDEX IF NOT EXISTS ix_raw_articles_fetched_at ON raw_articles(fetched_at);

CREATE TABLE IF NOT EXISTS reporting_units (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    day TIMESTAMPTZ NOT NULL,
    representative_article_id UUID NOT NULL REFERENCES raw_articles(id),
    article_count INTEGER NOT NULL DEFAULT 1,
    source_tiers JSONB NOT NULL DEFAULT '{}',
    owner_groups JSONB NOT NULL DEFAULT '{}',
    tier1_owner_groups JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_reporting_units_day ON reporting_units(day);
CREATE INDEX IF NOT EXISTS ix_reporting_units_rep_article_id ON reporting_units(representative_article_id);

CREATE TABLE IF NOT EXISTS stories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    day TIMESTAMPTZ NOT NULL,
    primary_entities JSONB NOT NULL DEFAULT '[]',
    tier1_unit_count INTEGER NOT NULL DEFAULT 0,
    tier2_unit_count INTEGER NOT NULL DEFAULT 0,
    tier3_unit_count INTEGER NOT NULL DEFAULT 0,
    tier4_unit_count INTEGER NOT NULL DEFAULT 0,
    distinct_owners INTEGER NOT NULL DEFAULT 0,
    viewpoint_cluster_id UUID,
    status story_status NOT NULL DEFAULT 'pending',
    gate_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_stories_day ON stories(day);
CREATE INDEX IF NOT EXISTS ix_stories_status ON stories(status);
CREATE INDEX IF NOT EXISTS ix_stories_viewpoint_cluster ON stories(viewpoint_cluster_id);

CREATE TABLE IF NOT EXISTS story_unit_links (
    id SERIAL PRIMARY KEY,
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    unit_id UUID NOT NULL REFERENCES reporting_units(id) ON DELETE CASCADE,
    UNIQUE(story_id, unit_id)
);

CREATE TABLE IF NOT EXISTS curated_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    platform VARCHAR(50) NOT NULL,
    caption TEXT NOT NULL,
    media_urls JSONB,
    source_urls JSONB NOT NULL,
    status curated_post_status NOT NULL DEFAULT 'draft',
    scheduled_at TIMESTAMPTZ,
    posted_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_curated_posts_status ON curated_posts(status);

CREATE TABLE IF NOT EXISTS status_log (
    id SERIAL PRIMARY KEY,
    run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    phase VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL,
    details JSONB,
    commit_sha VARCHAR(40)
);

CREATE INDEX IF NOT EXISTS ix_status_log_run_at ON status_log(run_at);

CREATE TABLE IF NOT EXISTS canonical_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name VARCHAR(255) NOT NULL,
    entity_type VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    location_type VARCHAR(50),
    geonames_id VARCHAR(50),
    geojson JSONB,
    geometry_id UUID,
    layer_id UUID
);

CREATE INDEX IF NOT EXISTS ix_canonical_entities_type ON canonical_entities(entity_type);
CREATE INDEX IF NOT EXISTS ix_canonical_entities_name ON canonical_entities(canonical_name);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id SERIAL PRIMARY KEY,
    canonical_entity_id UUID NOT NULL REFERENCES canonical_entities(id) ON DELETE CASCADE,
    alias VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(canonical_entity_id, alias)
);

CREATE INDEX IF NOT EXISTS ix_entity_aliases_alias ON entity_aliases(alias);

CREATE TABLE IF NOT EXISTS event_geometries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL,
    geometry_type geometry_type NOT NULL,
    geojson JSONB NOT NULL,
    properties JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_event_geometries_event_id ON event_geometries(event_id);

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

CREATE INDEX IF NOT EXISTS ix_event_layers_name ON event_layers(name);

CREATE TABLE IF NOT EXISTS events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    location_name VARCHAR(255),
    location_type VARCHAR(50),
    radius_km DOUBLE PRECISION,
    start_time TIMESTAMPTZ NOT NULL,
    event_type event_type NOT NULL DEFAULT 'other',
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    source_count INTEGER NOT NULL DEFAULT 0,
    tier1_source_count INTEGER NOT NULL DEFAULT 0,
    entities JSONB,
    geometry_id UUID REFERENCES event_geometries(id) ON DELETE SET NULL,
    layer_id UUID REFERENCES event_layers(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_events_story_id ON events(story_id);
CREATE INDEX IF NOT EXISTS ix_events_layer_id ON events(layer_id);
CREATE INDEX IF NOT EXISTS ix_events_event_type ON events(event_type);
CREATE INDEX IF NOT EXISTS ix_events_location ON events(latitude, longitude);
CREATE INDEX IF NOT EXISTS ix_events_start_time ON events(start_time);

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

CREATE TABLE IF NOT EXISTS snippets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    article_id UUID NOT NULL REFERENCES raw_articles(id) ON DELETE CASCADE,
    snippet_type snippet_type NOT NULL DEFAULT 'quote',
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

CREATE TABLE IF NOT EXISTS media_assets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id UUID REFERENCES raw_articles(id) ON DELETE CASCADE,
    story_id UUID REFERENCES stories(id) ON DELETE CASCADE,
    media_type media_type NOT NULL,
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_date ON source_reliability_snapshots(source_domain, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_source_reliability_source_date ON source_reliability_snapshots(source_domain, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_source_reliability_date ON source_reliability_snapshots(snapshot_date);

CREATE TABLE IF NOT EXISTS fact_check_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_domain VARCHAR(255) NOT NULL,
    article_id UUID REFERENCES raw_articles(id) ON DELETE SET NULL,
    claim TEXT NOT NULL,
    claim_hash CHAR(64) NOT NULL,
    verdict fact_check_verdict NOT NULL,
    confidence INTEGER NOT NULL DEFAULT 50,
    fact_checker fact_checker NOT NULL,
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
    severity correction_severity NOT NULL DEFAULT 'moderate',
    correction_date TIMESTAMPTZ NOT NULL,
    correction_url TEXT,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_correction_source_date ON correction_records(source_domain, correction_date);
CREATE INDEX IF NOT EXISTS ix_correction_article ON correction_records(article_id);
CREATE INDEX IF NOT EXISTS ix_correction_severity ON correction_records(severity);

CREATE TABLE IF NOT EXISTS claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    claim_type claim_type NOT NULL DEFAULT 'fact',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_claims_story_id ON claims(story_id);
CREATE INDEX IF NOT EXISTS ix_claims_claim_type ON claims(claim_type);

CREATE TABLE IF NOT EXISTS claim_evidence (
    id SERIAL PRIMARY KEY,
    claim_id UUID NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    unit_id UUID NOT NULL REFERENCES reporting_units(id) ON DELETE CASCADE,
    stance claim_stance NOT NULL DEFAULT 'neutral',
    confidence INTEGER NOT NULL DEFAULT 50,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(claim_id, unit_id)
);

CREATE INDEX IF NOT EXISTS ix_claim_evidence_unit_id ON claim_evidence(unit_id);

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

CREATE INDEX IF NOT EXISTS ix_entity_edges_subject ON entity_edges(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS ix_entity_edges_object ON entity_edges(object_type, object_id);
CREATE INDEX IF NOT EXISTS ix_entity_edges_predicate ON entity_edges(predicate);

CREATE TABLE IF NOT EXISTS topic_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    parent_group_id UUID REFERENCES topic_groups(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_topic_group_name_parent ON topic_groups(name, parent_group_id);
CREATE INDEX IF NOT EXISTS ix_topic_groups_parent ON topic_groups(parent_group_id);

CREATE TABLE IF NOT EXISTS story_topic_groups (
    id SERIAL PRIMARY KEY,
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    topic_group_id UUID NOT NULL REFERENCES topic_groups(id) ON DELETE CASCADE,
    confidence INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(story_id, topic_group_id)
);

CREATE TABLE IF NOT EXISTS source_topic_reliability (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_domain VARCHAR(255) NOT NULL,
    topic_group_id UUID NOT NULL REFERENCES topic_groups(id) ON DELETE CASCADE,
    score INTEGER NOT NULL,
    sample_size INTEGER NOT NULL DEFAULT 0,
    snapshot_date TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_str_sd_tg ON source_topic_reliability(source_domain, topic_group_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_str_lookup ON source_topic_reliability(source_domain, topic_group_id);
CREATE INDEX IF NOT EXISTS ix_str_date ON source_topic_reliability(snapshot_date);