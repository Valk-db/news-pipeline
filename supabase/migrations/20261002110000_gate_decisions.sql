-- Make the corroboration gate auditable: append-only gate_decisions.
--
-- apply_tier1_gate() and apply_dynamic_gate() decide whether a story may be curated, and the
-- only record of the decision is a sentence in stories.gate_reason. That sentence cannot answer
-- either question anyone actually asks of a gate:
--
--   "which owner groups corroborated this story, and from which articles?" -- the resolution
--       the gate already did is not recorded anywhere, so the answer means re-running it.
--   "what did the gate say about this story yesterday?" -- stories holds only the latest answer;
--       the next re-gate overwrites it, and the change of mind leaves no trace.
--
-- gate_decisions records one row per evaluation, with the evidence the evaluation counted:
-- the owner-group histogram (after collapsing wire copies to their origin), the contributing
-- articles with the wire_origin that made each collapse, and the score breakdown when the
-- dynamic gate decided. stories.gate_reason is still written exactly as before -- scripts and
-- the curation UI read it -- so nothing that depends on the sentence changes.
--
-- Append-only, and that is a rule rather than a hope: there is no UPDATE or DELETE path for
-- this table anywhere in the codebase, and no migration deletes from it. A decision is
-- corrected by re-gating, which appends the corrected decision and leaves the superseded one
-- readable as history. stories.gate_reason and stories.status are the mutable current state;
-- this is the ledger behind them. A foreign key with ON DELETE CASCADE is the one exception and
-- it is not an exception to the convention -- deleting a story must not leave rows describing a
-- story that no longer exists, and those rows are gone with the thing they described.
--
-- gate_name is 'tier1' (the boolean rule that moved the status) or 'dynamic' (the admission
-- score). A run in shadow mode writes both: the tier1 row is what decided, and the dynamic row
-- carries the score the dynamic gate would have used, marked breakdown->>'shadow' = 'true'.
-- score and pass_threshold are NULL for the tier1 rows because a boolean rule has no score.
--
-- analyzer_version/input_hash follow the P2 recomputable contract (migration
-- 20261002050000): gate_version says the payload shape, and analyzer_version says which gate
-- analyzer wrote the row. Unlike every other derived table this one is NOT owned by a
-- scripts/recompute_derived.py stage, because "recompute a decision" is not a delete followed by
-- a rewrite -- it is re-running the gate, which appends. See NOT_RECOMPUTED_TABLES in
-- src/shared/analyzer_versions.py.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, and CREATE INDEX IF NOT EXISTS, so re-applying is a
-- no-op. The index is the one access pattern this table exists to serve -- one story's decision
-- history, newest first. No index on the JSON columns: the questions are asked by hand during a
-- review, so a sequential scan is the right plan and a GIN index would be a write cost on every
-- gate run forever.

CREATE TABLE IF NOT EXISTS public.gate_decisions (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id              uuid NOT NULL REFERENCES public.stories(id) ON DELETE CASCADE,
    gate_name             text NOT NULL,
    gate_version          text NOT NULL,
    decided_at            timestamptz NOT NULL DEFAULT now(),
    passed                boolean NOT NULL,
    score                 integer NULL,
    pass_threshold        integer NULL,
    tier1_unit_count      integer NOT NULL,
    distinct_owners       integer NOT NULL,
    owner_groups          jsonb NOT NULL,
    contributing_articles jsonb NOT NULL,
    breakdown             jsonb NULL,
    analyzer_version      text NULL,
    input_hash            text NULL
);

COMMENT ON TABLE public.gate_decisions IS
    'Append-only ledger of corroboration-gate decisions: one row per evaluation, carrying the '
    'owner histogram and contributing articles the gate counted (wire copies collapsed to their '
    'origin). INSERT-only by convention -- a decision is corrected by re-gating, which appends. '
    'Written by src/verification/tiers.py.';

COMMENT ON COLUMN public.gate_decisions.breakdown IS
    'The dynamic gate score breakdown, with shadow:true when the score was computed in shadow '
    'mode and did not move the status. NULL for tier1 rows.';

COMMENT ON COLUMN public.gate_decisions.contributing_articles IS
    '[{article_id, url, source_domain, tier, wire_origin, content_hash}]. wire_origin is the '
    'wire-service domain a syndicated copy was attributed to, or NULL when the article is not a '
    'wire copy.';

COMMENT ON COLUMN public.gate_decisions.distinct_owners IS
    'Number of distinct owner groups AFTER collapsing wire copies to the wire origin that '
    'published the content. Always equal to the key count of owner_groups.';

CREATE INDEX IF NOT EXISTS ix_gate_decisions_story_decided_at
    ON public.gate_decisions (story_id, decided_at DESC);
