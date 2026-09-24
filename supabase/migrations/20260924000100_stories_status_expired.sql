-- Adds the EXPIRED label used by src/verification/cleanup.py. Idempotent.
-- Run this statement by itself in the Supabase SQL editor (a new enum label cannot be used in
-- the same transaction that adds it).
ALTER TYPE status ADD VALUE IF NOT EXISTS 'EXPIRED';