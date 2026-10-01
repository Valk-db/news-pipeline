-- Pipeline Runs Ledger Migration
-- One row per stage per pipeline execution, for observability of every stage.
-- Added on the procmon branch procmon/batch-trust.

-- run_id groups all stages of one pipeline execution. finished_at stays NULL while a
-- stage is running, so an interrupted run is visible as a row that never closed.
-- items_dropped_by_reason is a JSONB counter map, e.g. {"parse_failed": 3}.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL,
    stage VARCHAR(50) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    items_in INTEGER NOT NULL DEFAULT 0,
    items_out INTEGER NOT NULL DEFAULT 0,
    items_dropped_by_reason JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT
);

CREATE INDEX IF NOT EXISTS ix_pipeline_runs_run_id ON pipeline_runs(run_id);
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_stage ON pipeline_runs(stage);
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_started_at ON pipeline_runs(started_at);
