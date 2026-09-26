# AGENT_TASKS.md v20

Supersedes v19. **Phase 2 (P2-A/B/C/D) is done and merged** — commit `c038b0b`
on `main`. Independently verified against a fresh clone:

- `ruff check .` — clean.
- Full suite: 355 collected, 350 passed, 5 skipped (the 5 are the known
  Postgres-integration guard in `test_verification.py`, expected outside CI).
- Baseline before Phase 2 (commit `303816d`) was 330 collected — so Phase 2
  added ~25 net-new tests.
- `20260926000000_phase2_claims_and_graph.sql` matches the ORM models
  column-for-column, idempotent (`IF NOT EXISTS` throughout).
- `weekly-enrichment.yml`'s `phase2` job and the curation UI wiring (claim
  matrix, topic badges, narrative links) are present exactly as described.

**Correction to the record:** the Phase 2 completion summary claimed "277
tests pass." That number matches neither the pre-Phase2 baseline (330) nor
the post-Phase2 total (355) nor the net-new delta (~25) — no scenario
reconstructs it, and CI runs the identical bare `pytest tests/ -v --tb=short`
so it isn't an environment artifact. Not a regression, just don't trust that
class of self-reported number without a spot-check going forward.

**Merge policy (standing, unchanged):** once your own checks pass for a
task — `ruff check .` clean, targeted tests green — merge it yourself,
separate PR per task, same convention as Phase 1/2. Don't wait for review.

---

## Scope of this batch — and one finding that changes it

This batch is `GRAND_PLAN.md` §3, the verification & certification layer.
It's next because §2's `claims`/`topic_groups` just landed and §3 explicitly
builds on both.

**Verified finding that overrides part of §3's text:** `GRAND_PLAN.md` §3
says `source_topic_reliability` "extends `SourceReliabilitySnapshot`,
`FactCheckRecord`, `CorrectionRecord`." Checked all three:

- `SourceReliabilitySnapshot` is live — `scripts/run_weekly_reliability.py`
  calls `compute_daily_reliability_snapshots()` and is wired into
  `weekly-enrichment.yml`.
- `FactCheckRecord`/`CorrectionRecord` are **not** live. `fact_check_article()`
  in `src/reliability/fact_checker.py` exists but is called from nowhere in
  `src/` — only `tests/test_reliability.py::test_...` asserts it's
  `callable()`, which is an importability check, not a usage check. Nothing
  in ingestion, enrichment, or any scheduled workflow writes a
  `FactCheckRecord` or `CorrectionRecord` row. Both tables are effectively
  always empty right now.

Building P3-B against those two tables would ship a job that always finds
zero rows. **P3-B below is redesigned to score off `Claim`/`ClaimEvidence`
instead** (populated today, via `run_claim_extraction.py`) — a
claim-consensus-agreement signal per source/topic, not a fact-check-verdict
signal. This is a real scope change from the doc's original wording, not a
silent substitution — flag it as such in the PR description. Wiring
`fact_check_article()` into an actual scheduled job so `FactCheckRecord`
becomes real data is listed as backlog below — it's an LLM call per claim
and needs its own budget-governor decision, same as claim extraction did;
don't fold it into this task silently.

---

## P3-A — Schema: `source_topic_reliability`

### Goal
Per-(source, topic) reliability score, generalizing the single global
`SourceReliabilitySnapshot` per `GRAND_PLAN.md` §3.

### New migration
```sql
-- supabase/migrations/<next-timestamp>_source_topic_reliability.sql
CREATE TABLE IF NOT EXISTS source_topic_reliability (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_domain VARCHAR(255) NOT NULL,
    topic_group_id UUID NOT NULL REFERENCES topic_groups(id) ON DELETE CASCADE,
    score INTEGER NOT NULL,          -- 0-100, same convention as source_reliability_snapshots
    sample_size INTEGER NOT NULL DEFAULT 0,
    snapshot_date TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_domain, topic_group_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS ix_str_lookup ON source_topic_reliability(source_domain, topic_group_id);
CREATE INDEX IF NOT EXISTS ix_str_date ON source_topic_reliability(snapshot_date);
```
Checked first: zero existing hits for `source_topic_reliability` anywhere in
`supabase/migrations/` — this is a genuinely new table, no collision.

Mirror into `src/schema/models.py` as `SourceTopicReliability(Base)`,
placed directly after `StoryTopicGroup` (line ~647), same column
conventions as `SourceReliabilitySnapshot` (`Integer` 0-100 for score, not
`Float`).

### Acceptance
Model imports cleanly from `src.schema.models`; migration applies against a
scratch DB with no name collisions.

---

## P3-B — Reliability scoring job (claim-consensus based — see finding above)

### Goal
Populate `source_topic_reliability` from `Claim`/`ClaimEvidence`: for each
claim with ≥2 non-neutral evidence rows, find the confidence-weighted
majority stance; a unit's evidence that agrees with the majority is a point
*for* that unit's source, disagreement is a point *against*. Roll up by
`(source_domain, topic_group_id)`.

### Join path
`ClaimEvidence.claim_id → Claim.story_id → StoryTopicGroup.story_id →
topic_group_id` (direct — no need to route through articles/units for the
topic side). `ClaimEvidence.unit_id → ReportingUnit.representative_article_id
→ RawArticle.source_domain` for the source side (representative article's
domain stands in for the unit's source — same simplification the rest of
the codebase already makes for units).

### New file: `src/verification/reliability.py`
```python
"""Per-(source, topic) reliability scoring from claim-evidence consensus.

Not fact-check-verdict based -- see AGENT_TASKS.md P3-B for why
FactCheckRecord/CorrectionRecord aren't the source here (they're
unpopulated in production today).
"""
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import (
    Claim, ClaimEvidence, ClaimStance, ReportingUnit, RawArticle,
    StoryTopicGroup, SourceTopicReliability,
)

MIN_SAMPLE_SIZE = 3  # don't write a score off fewer than this many evidence rows


async def compute_source_topic_reliability(session: AsyncSession, lookback_days: int = 90) -> dict:
    results = {"pairs_scored": 0, "claims_considered": 0, "errors": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    stmt = (
        select(
            ClaimEvidence.claim_id, ClaimEvidence.stance, ClaimEvidence.confidence,
            StoryTopicGroup.topic_group_id, RawArticle.source_domain,
        )
        .join(Claim, Claim.id == ClaimEvidence.claim_id)
        .join(StoryTopicGroup, StoryTopicGroup.story_id == Claim.story_id)
        .join(ReportingUnit, ReportingUnit.id == ClaimEvidence.unit_id)
        .join(RawArticle, RawArticle.id == ReportingUnit.representative_article_id)
        .where(Claim.first_seen_at >= cutoff)
    )
    rows = (await session.execute(stmt)).all()

    by_claim: dict = defaultdict(list)
    for claim_id, stance, confidence, topic_group_id, source_domain in rows:
        by_claim[claim_id].append((stance, confidence, topic_group_id, source_domain))

    # pair_stats[(source_domain, topic_group_id)] = {"agree": weighted, "disagree": weighted, "n": count}
    pair_stats: dict = defaultdict(lambda: {"agree": 0.0, "disagree": 0.0, "n": 0})

    for claim_id, evidence in by_claim.items():
        non_neutral = [e for e in evidence if e[0] != ClaimStance.NEUTRAL]
        if len(non_neutral) < 2:
            continue  # no majority to measure agreement against
        results["claims_considered"] += 1
        support_w = sum(c for s, c, _, _ in non_neutral if s == ClaimStance.SUPPORTS)
        dispute_w = sum(c for s, c, _, _ in non_neutral if s == ClaimStance.DISPUTES)
        majority = ClaimStance.SUPPORTS if support_w >= dispute_w else ClaimStance.DISPUTES
        for stance, confidence, topic_group_id, source_domain in non_neutral:
            key = (source_domain, topic_group_id)
            pair_stats[key]["n"] += 1
            if stance == majority:
                pair_stats[key]["agree"] += confidence
            else:
                pair_stats[key]["disagree"] += confidence

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for (source_domain, topic_group_id), stats in pair_stats.items():
        if stats["n"] < MIN_SAMPLE_SIZE:
            continue  # thin-data floor -- no row rather than a noisy one
        total = stats["agree"] + stats["disagree"]
        score = round(100 * stats["agree"] / total) if total else 50

        existing_stmt = select(SourceTopicReliability).where(
            SourceTopicReliability.source_domain == source_domain,
            SourceTopicReliability.topic_group_id == topic_group_id,
            SourceTopicReliability.snapshot_date == today,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            existing.score, existing.sample_size = score, stats["n"]
        else:
            session.add(SourceTopicReliability(
                source_domain=source_domain, topic_group_id=topic_group_id,
                score=score, sample_size=stats["n"], snapshot_date=today,
            ))
        results["pairs_scored"] += 1

    await session.commit()
    return results
```

### Wiring
New script `scripts/compute_reliability.py`, same `init_db()` /
`get_session_maker()` / summary-dict / `GITHUB_OUTPUT` pattern as
`scripts/run_claim_extraction.py`. Add as a step in `weekly-enrichment.yml`'s
`phase2` job (rename it, or add a `phase3` job — your call, keep the name
honest about what it now covers). Zero LLM cost — no budget reason to run
it on a different cadence than weekly.

### Acceptance
New `tests/test_reliability_scoring.py` (name it distinctly from the
existing `tests/test_reliability.py`, which covers the fact-checker/consensus
code — don't collide). Cover: a source whose evidence agrees with the
claim majority scores high; a source that consistently disagrees scores
low; a `(source, topic)` pair below `MIN_SAMPLE_SIZE` gets no row; a claim
with only 1 non-neutral evidence row is skipped (no majority to compare
against); re-running same-day updates the existing row rather than
duplicating (exercises the `UNIQUE` constraint).

---

## P3-C — Dynamic gate: `admission_score` + `harm_level`

### Goal
Generalize the static tier-1 gate (`src/verification/tiers.py::evaluate_tier1_gate`,
~line 280) into `GRAND_PLAN.md` §3's scoring function:
`admission_score = f(source_tier_baseline, corroboration_count,
distinct_owner_count, virality_signal, harm_level)`.

**Locked decision (§7's open question):** `harm_level` is heuristic, not
LLM-classified, for v1 — matches the $0 budget stance, no new prompt needed.

### `harm_level` heuristic
New function in `src/verification/tiers.py`:
```python
async def compute_harm_level(session: AsyncSession, story: "Story") -> str:
    """"high" iff the story has a Claim with claim_type == ClaimType.ALLEGATION
    AND at least one id in story.primary_entities resolves to a
    CanonicalEntity with entity_type == "PERSON". Query CanonicalEntity
    directly (CanonicalEntity.id.in_(story.primary_entities)) rather than
    re-deriving entity types from raw article.entities -- primary_entities
    already stores canonical IDs post-canonicalization. Check whether
    primary_entities holds str or UUID values before writing the .in_()
    filter -- don't assume, read how it's compared elsewhere in stories.py.
    Returns "low" otherwise."""
```

### `admission_score`
Add `compute_admission_score()` alongside `evaluate_tier1_gate` —
**don't delete or change the old function's call sites yet.** Wire it into
`apply_tier1_gate()` behind a new `dynamic_gate_enabled: bool = False` in
`src/shared/config.py`, default off. This lets the score be compared
against the current boolean gate's real decisions before it's live.

```python
async def compute_admission_score(
    session: AsyncSession, story: "Story", tier1_unit_count: int, distinct_owners: int,
) -> tuple[int, dict]:
    """Returns (score 0-100, breakdown dict for gate_reason logging).

    virality_signal: max(unit.article_count for units on this story where
    "tier3" in (unit.source_tiers or {}) or "tier4" in (unit.source_tiers or {})),
    0 if none. source_tiers is a JSON dict ({"tier1": 2, ...}), not a scalar
    column -- check membership on the dict, don't compare it as an enum.

    harm_level from compute_harm_level() above: "high" raises the
    corroboration bar (higher required distinct_owners/tier1_unit_count for
    the same score) rather than blocking outright -- GRAND_PLAN §3 is
    explicit harm_level scales the bar, it doesn't gate alone.

    Calibrate weights against evaluate_tier1_gate's existing pass/fail line
    on real data before locking them in -- don't invent numbers from
    scratch. State the final weights as a table in the PR description, same
    as P2-D documented its Jaccard thresholds.
    """
```

### Acceptance
Extend `tests/test_tiers.py` (don't replace). Cover: `compute_harm_level`
returns `"high"` for a story with an `ALLEGATION` claim + a `PERSON`
canonical entity, `"low"` otherwise. `compute_admission_score` returns a
breakdown dict naming all 5 factors. Run it against the same fixture
stories `evaluate_tier1_gate` is already tested against and assert
`score >= 50` iff the old boolean gate passed on those specific fixtures —
this is a calibration check against known cases, not a formal guarantee.

---

## Order of work

1. P3-A first — schema is the dependency for B and C.
2. P3-B and P3-C can proceed in parallel once P3-A is on `main`.
3. Full `pytest` run after each task. Separate PRs, same convention as
   Phase 1/2. Merge yourself once green (merge policy above).
4. Before P3-C: read `src/verification/tiers.py`'s `evaluate_tier1_gate` and
   `apply_tier1_gate` in full (~lines 280–388) — the dynamic gate must reuse
   `apply_tier1_gate`'s existing batched unit query, not add a second
   N+1 query pattern beside it.

## Not in this batch (backlog for the next round)

- **Wiring `fact_check_article()` into a scheduled job** so
  `FactCheckRecord` becomes real data — an LLM call per claim, needs its
  own budget-governor decision (same shape as claim extraction's). Once
  live, `source_topic_reliability` could blend fact-check verdicts back in
  alongside the claim-consensus signal from P3-B.
- **Certification badge** (§3): natural follow-on once `admission_score`
  exists to derive `unconfirmed → corroborated → primary-source-verified`
  from — blocked on P3-C landing, not on any open design question.
- **Evidence snapshotting** and **media forensics** (§3): both need an
  infra decision `GRAND_PLAN.md` §7 leaves open (screenshot/image storage
  on a $0 budget) — worth a short design pass before speccing as tasks.
- **Phase 1a new source categories** (gov/legal docs, sensor feeds,
  non-English tier-1, OSINT tier, leaked datasets, podcast transcripts):
  none started. Ranked priority #1 in `GRAND_PLAN.md` §0, deliberately not
  in this batch since Phase 3 was already flagged as next after Phase 2.
- **Phase 4 globe expansion**: schema bones (`EventGeometry`/`EventLayer`/
  `Event`) exist; historical playback, non-point layer types, and
  sub-national geocoding are all still undone.