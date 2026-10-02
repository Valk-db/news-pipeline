-- Globe Events Schema Migration
-- Idempotent migration for event visualization tables and enums.
--
-- Type names and enum labels are the SQLAlchemy ones ('eventtype', 'geometrytype', and
-- the Python member NAMES), because the models are what bind them: src/schema/models.py
-- declares Enum(EventType, name="eventtype"), so a migration that created 'event_type'
-- with lowercase labels left a parallel type the application never wrote to. That is the
-- type-name race that made create_all win in dev; this file now agrees with the models,
-- and the column types match them too (double precision coordinates and json payloads,
-- not VARCHAR(50)/JSON).

-- Create enum types if they don't exist
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'eventtype' AND n.nspname = 'public') THEN
        CREATE TYPE eventtype AS ENUM (
            'CONFLICT', 'PROTEST', 'ELECTION', 'DISASTER', 'ACCIDENT',
            'POLITICAL', 'ECONOMIC', 'HEALTH', 'ENVIRONMENTAL', 'CRIME',
            'SPORTS', 'CULTURAL', 'SCIENTIFIC', 'OTHER'
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                   WHERE t.typname = 'geometrytype' AND n.nspname = 'public') THEN
        CREATE TYPE geometrytype AS ENUM (
            'POINT', 'POLYGON', 'LINESTRING', 'MULTIPOINT',
            'MULTIPOLYGON', 'MULTILINESTRING', 'GEOMETRYCOLLECTION'
        );
    END IF;
END $$;

-- Create event_geometries table
CREATE TABLE IF NOT EXISTS event_geometries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL,
    geometry_type geometrytype NOT NULL,
    geojson JSON NOT NULL,
    properties JSON,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Create event_layers table
CREATE TABLE IF NOT EXISTS event_layers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    description TEXT,
    filter_criteria JSON NOT NULL DEFAULT '{}',
    style JSON NOT NULL DEFAULT '{}',
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
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    location_name VARCHAR(255),
    location_type VARCHAR(50),
    radius_km DOUBLE PRECISION,
    start_time TIMESTAMPTZ NOT NULL,
    event_type eventtype NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 0,
    tier1_source_count INTEGER NOT NULL DEFAULT 0,
    entities JSON,
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
    ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS location_type VARCHAR(50),
    ADD COLUMN IF NOT EXISTS geonames_id VARCHAR(50),
    ADD COLUMN IF NOT EXISTS geojson JSON;

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

-- Close the anonymous REST API path, like every other table in the schema.
ALTER TABLE event_geometries ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_layers ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;

-- Seed the default event layer. ON CONFLICT DO NOTHING cannot do this job: event_layers
-- has no unique constraint, so it never fires and every re-run of this file would add
-- another 'default' row. Not-exists is the guard that actually holds.
INSERT INTO event_layers (id, name, description, filter_criteria, style, is_default, is_visible, min_zoom, max_zoom, color)
SELECT
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
WHERE NOT EXISTS (SELECT 1 FROM event_layers WHERE name = 'default');