-- Phase 2: claims, claim evidence, entity edges, topic groups
-- Idempotent migration -- see GRAND_PLAN.md §2
--
-- Enum type names and labels are the SQLAlchemy ones ('claimtype', 'claimstance',
-- 'edgepredicate') with the Python member NAMES as labels, because src/schema/models.py
-- declares Enum(ClaimType) and so binds the member name. A migration-created
-- 'claim_type' with lowercase labels would be a second type the application never
-- writes to, which is exactly the split-brain that let create_all win in dev.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'claimtype' AND n.nspname = 'public') THEN
        CREATE TYPE claimtype AS ENUM ('FACT', 'ALLEGATION', 'PREDICTION', 'QUOTE');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'claimstance' AND n.nspname = 'public') THEN
        CREATE TYPE claimstance AS ENUM ('SUPPORTS', 'DISPUTES', 'NEUTRAL');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'edgepredicate' AND n.nspname = 'public') THEN
        CREATE TYPE edgepredicate AS ENUM (
            'CORROBORATES', 'DISPUTES', 'SAME_EVENT_AS', 'PART_OF_NARRATIVE', 'CAUSED_BY'
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    claim_type claimtype NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    id SERIAL PRIMARY KEY,
    claim_id UUID NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    unit_id UUID NOT NULL REFERENCES reporting_units(id) ON DELETE CASCADE,
    stance claimstance NOT NULL,
    confidence INTEGER NOT NULL DEFAULT 50,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_claim_unit UNIQUE (claim_id, unit_id)
);

CREATE TABLE IF NOT EXISTS entity_edges (
    id SERIAL PRIMARY KEY,
    subject_type VARCHAR(20) NOT NULL,
    subject_id UUID NOT NULL,
    predicate edgepredicate NOT NULL,
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

ALTER TABLE claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE claim_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE entity_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE topic_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE story_topic_groups ENABLE ROW LEVEL SECURITY;