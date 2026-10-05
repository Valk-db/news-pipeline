-- Migration: Rename short token counter names to long form
-- Date: 2026-10-05
-- 
-- DEPLOY ORDER:
--   1. Stop the old application process (the one writing groq_tokens etc.)
--   2. Run this script in the Supabase SQL editor
--   3. Check the output of the verification queries
--   4. Start the new code (which writes groq_request_tokens etc.)
--
-- TIMING: Avoid running within 5 minutes of 00:00 UTC (8:00 PM EDT),
-- i.e. avoid 23:55 to 00:05 UTC. The day boundary is when the app
-- switches to a new row, and a migration at that moment could miss it.
--
-- BACKGROUND:
-- Commit c0c9b77 renamed token counters from short form (groq_tokens)
-- to long form (groq_request_tokens) to match the request counter naming.
-- Production still has spend under the old names. This migration moves
-- today's UTC-day rows to the new names, summing if both exist.
--
-- Only the current UTC day is touched. Yesterday's rows are history.

-- Backup first (durable, in the database)
CREATE TABLE IF NOT EXISTS budget_counters_bak_20261005 AS
SELECT * FROM budget_counters
WHERE day >= (now() AT TIME ZONE 'UTC')::date - 1;

BEGIN;
LOCK TABLE budget_counters IN SHARE ROW EXCLUSIVE MODE;

-- Check before: show what we're about to migrate
SELECT name, day, used FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date
  AND name IN (
    'groq_tokens', 'groq_request_tokens',
    'cerebras_tokens', 'cerebras_request_tokens',
    'openrouter_gemma_tokens', 'openrouter_gemma_request_tokens',
    'openrouter_nemotron_tokens', 'openrouter_nemotron_request_tokens',
    'groq_unpriced_calls', 'groq_request_unpriced_calls',
    'cerebras_unpriced_calls', 'cerebras_request_unpriced_calls',
    'openrouter_gemma_unpriced_calls', 'openrouter_gemma_request_unpriced_calls',
    'openrouter_nemotron_unpriced_calls', 'openrouter_nemotron_request_unpriced_calls'
  )
ORDER BY name;

-- groq tokens: merge old into new, summing if both exist
INSERT INTO budget_counters (name, day, used)
SELECT 'groq_request_tokens', day, SUM(used)
FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date
  AND name IN ('groq_tokens', 'groq_request_tokens')
GROUP BY day
ON CONFLICT (name, day) DO UPDATE SET used = EXCLUDED.used;
DELETE FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date AND name = 'groq_tokens';

-- cerebras tokens
INSERT INTO budget_counters (name, day, used)
SELECT 'cerebras_request_tokens', day, SUM(used)
FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date
  AND name IN ('cerebras_tokens', 'cerebras_request_tokens')
GROUP BY day
ON CONFLICT (name, day) DO UPDATE SET used = EXCLUDED.used;
DELETE FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date AND name = 'cerebras_tokens';

-- openrouter_gemma tokens
INSERT INTO budget_counters (name, day, used)
SELECT 'openrouter_gemma_request_tokens', day, SUM(used)
FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date
  AND name IN ('openrouter_gemma_tokens', 'openrouter_gemma_request_tokens')
GROUP BY day
ON CONFLICT (name, day) DO UPDATE SET used = EXCLUDED.used;
DELETE FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date AND name = 'openrouter_gemma_tokens';

-- openrouter_nemotron tokens
INSERT INTO budget_counters (name, day, used)
SELECT 'openrouter_nemotron_request_tokens', day, SUM(used)
FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date
  AND name IN ('openrouter_nemotron_tokens', 'openrouter_nemotron_request_tokens')
GROUP BY day
ON CONFLICT (name, day) DO UPDATE SET used = EXCLUDED.used;
DELETE FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date AND name = 'openrouter_nemotron_tokens';

-- groq unpriced calls
INSERT INTO budget_counters (name, day, used)
SELECT 'groq_request_unpriced_calls', day, SUM(used)
FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date
  AND name IN ('groq_unpriced_calls', 'groq_request_unpriced_calls')
GROUP BY day
ON CONFLICT (name, day) DO UPDATE SET used = EXCLUDED.used;
DELETE FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date AND name = 'groq_unpriced_calls';

-- cerebras unpriced calls
INSERT INTO budget_counters (name, day, used)
SELECT 'cerebras_request_unpriced_calls', day, SUM(used)
FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date
  AND name IN ('cerebras_unpriced_calls', 'cerebras_request_unpriced_calls')
GROUP BY day
ON CONFLICT (name, day) DO UPDATE SET used = EXCLUDED.used;
DELETE FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date AND name = 'cerebras_unpriced_calls';

-- openrouter_gemma unpriced calls
INSERT INTO budget_counters (name, day, used)
SELECT 'openrouter_gemma_request_unpriced_calls', day, SUM(used)
FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date
  AND name IN ('openrouter_gemma_unpriced_calls', 'openrouter_gemma_request_unpriced_calls')
GROUP BY day
ON CONFLICT (name, day) DO UPDATE SET used = EXCLUDED.used;
DELETE FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date AND name = 'openrouter_gemma_unpriced_calls';

-- openrouter_nemotron unpriced calls
INSERT INTO budget_counters (name, day, used)
SELECT 'openrouter_nemotron_request_unpriced_calls', day, SUM(used)
FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date
  AND name IN ('openrouter_nemotron_unpriced_calls', 'openrouter_nemotron_request_unpriced_calls')
GROUP BY day
ON CONFLICT (name, day) DO UPDATE SET used = EXCLUDED.used;
DELETE FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date AND name = 'openrouter_nemotron_unpriced_calls';

-- Verification: after migration, old names should be gone, new names should have the sums
-- Run this separately to verify (read-only):
-- SELECT name, day, used FROM budget_counters
-- WHERE day = (now() AT TIME ZONE 'UTC')::date
--   AND (name LIKE '%tokens' OR name LIKE '%unpriced_calls')
-- ORDER BY name;

COMMIT;

-- Post-commit verification (read-only, run after COMMIT)
SELECT name, day, used FROM budget_counters
WHERE day = (now() AT TIME ZONE 'UTC')::date
  AND (name LIKE '%tokens' OR name LIKE '%unpriced_calls')
ORDER BY name;
