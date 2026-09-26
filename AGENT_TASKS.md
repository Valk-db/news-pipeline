# AGENT_TASKS.md v19

Supersedes v18. **P2-A is done and merged** — commit `01985b3` on `main`
("P2-A: Add claim, claim_evidence, entity_edges, topic_groups schema").
Independently verified against a fresh clone (not just taken on your word):

- `ruff check .` — clean across the whole repo.
- All 8 new symbols (`ClaimType`, `Claim`, `ClaimStance`, `ClaimEvidence`,
  `EdgePredicate`, `EntityEdge`, `TopicGroup`, `StoryTopicGroup`) import
  cleanly from `src.schema.models`.
- `supabase/migrations/20260926000000_phase2_claims_and_graph.sql` matches
  the ORM models column-for-column and is idempotent (`IF NOT EXISTS` guards
  throughout).
- Full `pytest tests/` run: 254 passed, 5 skipped. The 6 failures seen were
  all in `test_ner.py` and are a sandbox-only gap (missing the
  `en_core_web_sm` spacy model download), not a regression — worth a quick
  check that CI's cache actually has that model, but it's not a P2-A defect.

**Merge policy (standing instruction, applies to every task below):** once
your own checks pass for a task — `ruff check .` clean, and that task's
targeted tests green — merge it yourself. Separate PR per task (P2-B, P2-C,
P2-D), same convention P2-A used. Don't stop and wait for review before
merging; verified-passing work merges itself.

---

## P2-B — Claim extraction pipeline

### Goal

Populate `claims` + `claim_evidence` for a story: one LLM call per story
(not per unit -- budget-conscious, matches `cluster_viewpoints`'s existing
pattern of capping at 10 unit texts per prompt) that extracts the atomic
claims being made and, for each claim, which units support/dispute/are
neutral on it.

**Trigger point matters for budget**: only run this on stories that have
already passed `apply_tier1_gate` (status `QUEUED`), not on every story as
it's created. An unqueued/blocked story never gets published, so spending a
Groq call on its claim matrix is wasted budget. Wire this as a periodic job
alongside enrichment, not inline in `run_ingestion()`.

### Refactor first: extract the shared unit-gathering helper

`src/verification/stories.py`'s `cluster_viewpoints()` (lines ~163-188) has
inline logic that loads a story's units, fetches each unit's representative
article, and builds a capped/truncated list of `{unit_id, text, source_tier}`
dicts. Extract this into a standalone function in the same file:

```python
async def _gather_unit_texts_for_story(
    session: AsyncSession, story: "Story", max_units: int = 10, max_chars: int = 2000,
) -> list[dict]:
    """Load up to max_units representative-article texts for a story's units,
    truncated to max_chars each. Shared by cluster_viewpoints() and the claim
    extraction pipeline (src/verification/claims.py) -- keep this the single
    source of truth for "what text do we show the LLM about a story."
    """
    ...  # move the existing inline logic here unchanged, just parameterize
    # max_units (cluster_viewpoints currently hardcodes [:10] on the prompt
    # build step, not on the gathering step -- fix that inconsistency here:
    # cap during gathering, not just during prompt assembly)
```

Update `cluster_viewpoints()` to call this helper instead of its inline block.
**This must not change `cluster_viewpoints()`'s existing behavior** --
`tests/test_viewpoint_clustering.py` must pass unchanged after the extraction.

### New file: `src/verification/claims.py`

```python
"""Claim extraction: populate claims + claim_evidence for gated stories.

One LLM call per story (not per unit) -- see AGENT_TASKS.md P2-B for why.
"""
import json
import logging
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import Story, Claim, ClaimEvidence, ClaimType, ClaimStance
from src.verification.stories import _gather_unit_texts_for_story

logger = logging.getLogger(__name__)

CLAIM_EXTRACTION_PROMPT = """Analyze these news article excerpts about the same event and extract the atomic factual claims being made.

For each distinct claim, identify:
- The claim text (one sentence, self-contained)
- claim_type: one of "fact", "allegation", "prediction", "quote"
- Which sources support it, dispute it, or are neutral/don't mention it

Articles:
{texts_for_prompt}

Return ONLY a JSON object of this exact shape, no explanation, no markdown fences:
{{
  "claims": [
    {{
      "text": "...",
      "claim_type": "fact",
      "evidence": [
        {{"unit_id": "...", "stance": "supports", "confidence": 80}}
      ]
    }}
  ]
}}
"""


async def extract_claims_for_story(session: AsyncSession, story_id: uuid.UUID) -> dict:
    """Extract and persist the claim matrix for one story.

    Returns a result dict with counts, matching the shape enrich_story()
    in src/enrichment/pipeline.py uses (errors: list, plus per-type counts)
    so this slots into the same reporting pattern as weekly enrichment.
    """
    results = {"story_id": str(story_id), "claims_created": 0, "evidence_created": 0, "errors": []}

    stmt = select(Story).where(Story.id == story_id)
    story = (await session.execute(stmt)).scalar_one_or_none()
    if not story:
        results["errors"].append("Story not found")
        return results

    unit_texts = await _gather_unit_texts_for_story(session, story, max_units=10)
    if len(unit_texts) < 2:
        # Not enough units for a meaningful claim matrix -- same threshold
        # philosophy as cluster_viewpoints, doesn't need to match exactly.
        return results

    texts_for_prompt = "\n\n---\n\n".join(
        f"unit_id: {u['unit_id']}\nSource: {u['source_tier']}\n{u['text']}" for u in unit_texts
    )

    from src.shared.llm import get_llm_client
    llm = await get_llm_client()

    try:
        result = await llm.chat_completion(
            messages=[{"role": "user", "content": CLAIM_EXTRACTION_PROMPT.format(texts_for_prompt=texts_for_prompt)}],
            temperature=0.1,
            max_tokens=1500,
        )
        # Use llm._parse_json_response, not raw json.loads -- it strips
        # fenced code blocks. cluster_viewpoints uses raw json.loads and is
        # fragile to this; don't repeat that in new code.
        data = llm._parse_json_response(result["choices"][0]["message"]["content"])

        for claim_data in data.get("claims", []):
            claim = Claim(
                story_id=story_id,
                text=claim_data["text"],
                claim_type=ClaimType(claim_data.get("claim_type", "fact")),
            )
            session.add(claim)
            await session.flush()  # get claim.id before adding evidence rows
            results["claims_created"] += 1

            for ev in claim_data.get("evidence", []):
                try:
                    evidence = ClaimEvidence(
                        claim_id=claim.id,
                        unit_id=uuid.UUID(ev["unit_id"]),
                        stance=ClaimStance(ev.get("stance", "neutral")),
                        confidence=int(ev.get("confidence", 50)),
                    )
                    session.add(evidence)
                    results["evidence_created"] += 1
                except (KeyError, ValueError) as e:
                    # A malformed evidence row shouldn't drop the whole claim
                    logger.warning(f"Skipping malformed evidence for claim {claim.id}: {e}")

        await session.commit()
    except Exception as e:
        logger.warning(f"Claim extraction failed for story {story_id}: {e}", exc_info=True)
        results["errors"].append(str(e))

    return results


async def extract_claims_for_recent_stories(
    session_factory, hours_back: int = 168, max_stories: int = 100,
) -> list[dict]:
    """Batch entry point -- mirrors enrich_recent_stories() in
    src/enrichment/pipeline.py. Only processes QUEUED stories (see P2-B goal:
    don't spend budget on stories that never got past the gate)."""
    ...  # follow enrich_recent_stories()'s existing query-and-loop pattern in
    # src/enrichment/pipeline.py, filtering Story.status == Story.Status.QUEUED
    # instead of whatever filter enrich_recent_stories uses -- read that
    # function before writing this one, don't guess its structure
```

### Wiring

- New script `scripts/run_claim_extraction.py`, modeled directly on
  `scripts/run_weekly_enrichment.py` (same `init_db()` / `get_session_maker()`
  / summary-dict / `GITHUB_OUTPUT` pattern).
- Add a step to `.github/workflows/weekly-enrichment.yml` calling the new
  script, OR a new dedicated workflow if enrichment and claim extraction
  should run on different schedules — check the existing workflow's schedule
  and comment on why before deciding; don't just bolt it on without checking
  whether weekly enrichment's cadence actually fits claim extraction's needs.

### Acceptance

New `tests/test_claim_extraction.py`, modeled on the existing
`tests/test_viewpoint_clustering.py` (same mocking style for `get_llm_client`
and the DB session). Cover: a story with enough units produces claims +
evidence rows; a malformed LLM response (bad JSON, missing fields) doesn't
crash the whole batch, just skips that story/claim and records it in
`results["errors"]`; a story below the unit threshold produces zero claims
without error. `tests/test_viewpoint_clustering.py` must still pass unchanged
after the `_gather_unit_texts_for_story` extraction.

---

## P2-C — Topic groups: seed + assignment

### Goal

Give every story a topic-group assignment so `source_topic_reliability`
(Phase 3) has something to key off of. Two parts: seed an initial hierarchy,
then assign stories to it.

### Seed hierarchy

New script `scripts/seed_topic_groups.py` (one-time, idempotent via
`ON CONFLICT DO NOTHING` matching the `event_layers` seed pattern in the
globe migration). Start narrow -- this grows organically, don't over-design a
deep taxonomy up front:

```
Geopolitics
  Geopolitics > Middle East
  Geopolitics > Europe
  Geopolitics > East Asia
Economy
  Economy > Markets
  Economy > Trade Policy
Health
Environment & Disaster
Technology
Domestic Politics (US)
```

### Assignment

New function `assign_story_topic_groups(session, story_id)` in
`src/verification/claims.py` (or a new `src/verification/topics.py` if
`claims.py` is getting large -- agent's judgment, but pick one and be
consistent). For v1, use `story.primary_entities` (already populated by
`build_stories()`) against the topic group names with simple keyword/entity
matching rather than another LLM call -- Phase 2's LLM budget is already
spent on claim extraction; don't double up without a clear reason.
Flag explicitly in the PR: this is a v1 heuristic, upgrading to
LLM-classified or claim-informed assignment is a fine follow-up once
`claims` data exists to inform it.

### Acceptance

Every `QUEUED` story gets at least one `story_topic_groups` row (falling back
to a generic top-level group if nothing matches -- don't leave stories
unassigned). New test file covering the keyword-matching logic against a
handful of known entity sets.

---

## P2-D — Narrative-arc `entity_edges`

### Goal

Link a story to an earlier one covering the same ongoing situation, beyond
the current 48h story-grouping window in `build_stories()`. This is the
`SAME_EVENT_AS` / `PART_OF_NARRATIVE` edges from `GRAND_PLAN.md` §2.

### Approach

New function `link_narrative_arcs(session, story_id)` — for a newly-created
or newly-gated story, look back further than 48h (start with 90 days) at
other stories sharing entities from `story.primary_entities` above some
overlap threshold. Reuse whatever entity-overlap logic `build_stories()`
already has for its own 48h window rather than writing a second
implementation — read `build_stories()` first. Write a
`subject_type="story"/object_type="story"` `EntityEdge` row with
`predicate=SAME_EVENT_AS` for a strong entity-overlap match, or
`PART_OF_NARRATIVE` for a weaker one — pick a concrete threshold (e.g. entity
Jaccard >= 0.5 for SAME_EVENT_AS, >= 0.2 for PART_OF_NARRATIVE) and state it
in the PR description so it's an explicit, tunable constant, not a buried
magic number.

### Acceptance

New test file. Cover: a story with strong entity overlap to an older one
gets a `SAME_EVENT_AS` edge; weak overlap gets `PART_OF_NARRATIVE`; no
overlap gets no edge; a story doesn't get edged to itself.

---

## Order of work

1. P2-A is done — skip it.
2. P2-B, P2-C, P2-D can proceed in parallel (or sequentially in one branch,
   your call) now that the schema is live on `main`.
3. Run the full pytest suite after each task. Land as separate, revertable
   PRs, same as Phase 1 and P2-A — and merge each one yourself once its
   checks are green (see the merge policy note at the top).
4. Before starting, read `src/enrichment/pipeline.py`'s `enrich_recent_stories`
   and `src/verification/stories.py`'s `build_stories` in full — both are
   referenced above as "reuse this pattern" and neither is reproduced in full
   here; guessing their structure instead of reading it is how Phase 1's
   mock-patching bug happened.