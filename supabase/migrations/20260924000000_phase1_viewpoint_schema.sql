-- Phase 1 (viewpoint aggregation) schema changes. Idempotent: safe to re-run.
-- Apply in the Supabase SQL editor as TWO separate executions, in this order.

-- EXECUTION A (run this statement by itself; a new enum label cannot be used in the
-- same transaction that adds it)
ALTER TYPE sourcetier ADD VALUE IF NOT EXISTS 'TIER4';

-- EXECUTION B
ALTER TABLE stories ADD COLUMN IF NOT EXISTS tier3_unit_count integer NOT NULL DEFAULT 0;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS tier4_unit_count integer NOT NULL DEFAULT 0;
ALTER TABLE stories ADD COLUMN IF NOT EXISTS viewpoint_cluster_id uuid;
CREATE INDEX IF NOT EXISTS ix_stories_viewpoint_cluster ON stories (viewpoint_cluster_id);