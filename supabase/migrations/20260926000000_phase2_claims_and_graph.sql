-- Phase 2: claims, claim evidence, entity edges, topic groups
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
CREATE INDEX IF NOT EXISTS ix_topic_groups_parent ON topic_groups(parent_group_id);