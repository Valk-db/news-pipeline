-- Phase 1 (viewpoint aggregation) schema changes. Idempotent: safe to re-run.
-- Migration B: Add columns and index (runs after enum is committed)
ALTER TABLE stories ADD COLUMN IF NOT EXISTS tier3_unit_count integer NOT NULL DEFAULT 0;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS tier4_unit_count integer NOT NULL DEFAULT 0;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS viewpoint_cluster_id uuid;
CREATE INDEX IF NOT EXISTS ix_stories_viewpoint_cluster ON stories (viewpoint_cluster_id);