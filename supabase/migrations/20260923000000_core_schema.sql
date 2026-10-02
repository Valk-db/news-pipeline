-- Core schema: the baseline every later migration assumes.
--
-- Why this file exists. Until now nothing under supabase/migrations/ could create
-- raw_articles, reporting_units, stories or canonical_entities -- applying the
-- migrations to an empty database failed on the first one -- so a database could only
-- come into existence through Base.metadata.create_all. That is how the dev database
-- was built, and it is why its enum types carry SQLAlchemy's names ('sourcetier',
-- 'eventtype', 'claimtype') while the migration files were written against other names
-- ('event_type', 'claim_type'): two owners of one schema, with create_all -- which ran
-- on every ingest -- always winning. Migrations own the schema now and create_all is
-- gone, so this baseline is what a fresh database is built from.
--
-- The DDL is generated from the models, not hand-written, so it cannot drift from
-- src/schema/models.py without the generated text changing:
--
--   python -c "from sqlalchemy.schema import CreateTable, CreateIndex; \
--     from sqlalchemy.dialects import postgresql; \
--     from src.schema.models import RawArticle; \
--     d = postgresql.dialect(); \
--     print(CreateTable(RawArticle.__table__).compile(dialect=d)); \
--     [print(CreateIndex(i).compile(dialect=d)) for i in RawArticle.__table__.indexes]"
--
-- Enum labels are the Python member NAMES, which is what SQLAlchemy binds for an
-- Enum() column that does not declare values_callable. Columns carry no server
-- DEFAULT because the models set their defaults in Python; adding some here would make
-- a migrations-built database differ from a create_all-built one.
--
-- The shape is deliberately "as of before the feature migrations": the columns those
-- files add (raw_articles.terminal_state and the scheme u1 and translation blocks, the
-- canonical_entities geolocation columns, stories.tier3_unit_count and the viewpoint
-- columns) are theirs to add, exactly as they already were.
--
-- Idempotent: safe to run more than once, and safe against a database create_all
-- already built, where every statement is a no-op.

-- ---------------------------------------------------------------------------
-- Enum types
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'sourcetier' AND n.nspname = 'public') THEN
        CREATE TYPE sourcetier AS ENUM ('TIER1', 'TIER2', 'TIER3', 'TIER4');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'status' AND n.nspname = 'public') THEN
        CREATE TYPE status AS ENUM ('PENDING', 'QUEUED', 'POSTED', 'REJECTED', 'BLOCKED');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'curated_post_status' AND n.nspname = 'public') THEN
        CREATE TYPE curated_post_status AS ENUM ('DRAFT', 'APPROVED', 'POSTED', 'FAILED');
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS raw_articles (
    id UUID NOT NULL,
    url TEXT NOT NULL,
    url_hash VARCHAR(64) NOT NULL,          -- SHA256 hex, the identity every dedup set uses
    title TEXT NOT NULL,
    body_text TEXT,
    summary TEXT,
    source_domain VARCHAR(255) NOT NULL,
    source_tier sourcetier NOT NULL,
    published_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    entities JSON,                          -- {"PERSON": [...], "ORG": [...], "GPE": [...]}
    minhash_signature JSON,                 -- MinHash serialized
    content_hash VARCHAR(64),               -- exact dedup
    -- No REFERENCES clause: reporting_units points back here too, so the two
    -- constraints are added together at the end of this file.
    reporting_unit_id UUID,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_raw_articles_published_at ON raw_articles (published_at);
CREATE INDEX IF NOT EXISTS ix_raw_articles_source_domain ON raw_articles (source_domain);
CREATE UNIQUE INDEX IF NOT EXISTS ix_raw_articles_url_hash ON raw_articles (url_hash);

CREATE TABLE IF NOT EXISTS reporting_units (
    id UUID NOT NULL,
    day TIMESTAMPTZ NOT NULL,               -- date bucket (UTC midnight)
    representative_article_id UUID NOT NULL,   -- FK to raw_articles added at the end of this file
    article_count INTEGER NOT NULL,
    source_tiers JSON NOT NULL,             -- {"tier1": 2, "tier2": 1}
    owner_groups JSON NOT NULL,             -- {"AP": 1, "Sinclair": 3, ...}
    tier1_owner_groups JSON NOT NULL,       -- owners from tier-1 articles only
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_reporting_units_day ON reporting_units (day);
CREATE INDEX IF NOT EXISTS ix_reporting_units_rep_article_id
    ON reporting_units (representative_article_id);

CREATE TABLE IF NOT EXISTS stories (
    id UUID NOT NULL,
    day TIMESTAMPTZ NOT NULL,
    primary_entities JSON NOT NULL,         -- top-N entity sets that defined this story
    tier1_unit_count INTEGER NOT NULL,
    tier2_unit_count INTEGER NOT NULL,
    distinct_owners INTEGER NOT NULL,
    status status NOT NULL,
    gate_reason TEXT,                       -- why blocked/queued
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_stories_day ON stories (day);
CREATE INDEX IF NOT EXISTS ix_stories_status ON stories (status);

CREATE TABLE IF NOT EXISTS story_unit_links (
    id SERIAL NOT NULL,
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    unit_id UUID NOT NULL REFERENCES reporting_units(id) ON DELETE CASCADE,
    PRIMARY KEY (id),
    CONSTRAINT uq_story_unit UNIQUE (story_id, unit_id)
);

CREATE TABLE IF NOT EXISTS curated_posts (
    id UUID NOT NULL,
    story_id UUID NOT NULL REFERENCES stories(id),
    platform VARCHAR(50) NOT NULL,          -- twitter, instagram, linkedin, threads, bluesky
    caption TEXT NOT NULL,
    media_urls JSON,                        -- [{"type": "image", "url": "...", "alt": "..."}]
    source_urls JSON NOT NULL,              -- canonical source URLs for attribution
    status curated_post_status NOT NULL,
    scheduled_at TIMESTAMPTZ,
    posted_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_curated_posts_status ON curated_posts (status);

CREATE TABLE IF NOT EXISTS status_log (
    id SERIAL NOT NULL,
    run_at TIMESTAMPTZ NOT NULL,
    phase VARCHAR(50) NOT NULL,             -- ingest, verify, group, curate
    status VARCHAR(20) NOT NULL,            -- ok, warn, error
    details JSON,
    commit_sha VARCHAR(40),
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_status_log_run_at ON status_log (run_at);

CREATE TABLE IF NOT EXISTS canonical_entities (
    id UUID NOT NULL,
    canonical_name VARCHAR(255) NOT NULL,   -- preferred display name
    entity_type VARCHAR(50) NOT NULL,       -- PERSON, ORG, GPE, LOC, EVENT, PRODUCT
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_canonical_entities_name ON canonical_entities (canonical_name);
CREATE INDEX IF NOT EXISTS ix_canonical_entities_type ON canonical_entities (entity_type);

-- The one pair of mutually referential tables: raw_articles names its unit, and a unit
-- names the article that represents it. Created as plain columns and constrained here,
-- because neither table can be created first with its foreign key inline.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'raw_articles_reporting_unit_id_fkey') THEN
        ALTER TABLE raw_articles ADD CONSTRAINT raw_articles_reporting_unit_id_fkey
            FOREIGN KEY (reporting_unit_id) REFERENCES reporting_units(id);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'reporting_units_representative_article_id_fkey') THEN
        ALTER TABLE reporting_units ADD CONSTRAINT reporting_units_representative_article_id_fkey
            FOREIGN KEY (representative_article_id) REFERENCES raw_articles(id);
    END IF;
END $$;

-- Close the anonymous REST API path on every table, which is what
-- scripts/enable_rls.py checks for: the service-role backend bypasses RLS, and a table
-- with no policies has no anon access.
ALTER TABLE raw_articles ENABLE ROW LEVEL SECURITY;
ALTER TABLE reporting_units ENABLE ROW LEVEL SECURITY;
ALTER TABLE stories ENABLE ROW LEVEL SECURITY;
ALTER TABLE story_unit_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE curated_posts ENABLE ROW LEVEL SECURITY;
ALTER TABLE status_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_entities ENABLE ROW LEVEL SECURITY;