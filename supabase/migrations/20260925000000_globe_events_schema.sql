-- Globe Events Schema Migration
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
ON CONFLICT DO NOTHING;