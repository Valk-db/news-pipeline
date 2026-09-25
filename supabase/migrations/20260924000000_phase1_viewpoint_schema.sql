-- Phase 1 (viewpoint aggregation) schema changes. Idempotent: safe to re-run.
-- Migration A: Add TIER4 enum value (must be in its own transaction)
ALTER TYPE sourcetier ADD VALUE IF NOT EXISTS 'TIER4';