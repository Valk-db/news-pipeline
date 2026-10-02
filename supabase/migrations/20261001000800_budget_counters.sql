-- Daily budget counters: the one table the LLM and translation budgets live in.
--
-- Both budgets used to be per-process (an int in llm_budget.py, a JSON file under var/
-- on an ephemeral runner), so "900 Groq requests a day" and "45,000 characters a day"
-- were really per-run and two runs a day could spend twice the documented quota. A
-- counter row per (name, day) is the only thing that makes the cap a fact about the
-- pipeline rather than about the process that happens to be running.
--
-- One row per day per budget, so the day rollover is a new row rather than a reset
-- that two processes could race. src/shared/budget.py spends with a single
-- INSERT .. ON CONFLICT DO UPDATE .. RETURNING, which is atomic across processes: no
-- read-then-write window for two runners to interleave in.
--
-- Idempotent: safe to run more than once.

CREATE TABLE IF NOT EXISTS budget_counters (
    name TEXT NOT NULL,                     -- 'groq_requests', 'mymemory_chars'
    day DATE NOT NULL,                      -- UTC day the spend belongs to
    used BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (name, day)
);

-- One day of one budget, for the rare "what did we spend today" question.
CREATE INDEX IF NOT EXISTS ix_budget_counters_day ON budget_counters (day);

ALTER TABLE budget_counters ENABLE ROW LEVEL SECURITY;