-- Source Topic Reliability Schema Migration
-- Per-(source_domain, topic_group) reliability score
-- See AGENT_TASKS.md v20 P3-A

CREATE TABLE IF NOT EXISTS source_topic_reliability (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_domain VARCHAR(255) NOT NULL,
    topic_group_id UUID NOT NULL REFERENCES topic_groups(id) ON DELETE CASCADE,
    score INTEGER NOT NULL,          -- 0-100, same convention as source_reliability_snapshots
    sample_size INTEGER NOT NULL DEFAULT 0,
    snapshot_date TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_domain, topic_group_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS ix_str_lookup ON source_topic_reliability(source_domain, topic_group_id);
CREATE INDEX IF NOT EXISTS ix_str_date ON source_topic_reliability(snapshot_date);