# AGENT_TASKS.md v18

Supersedes v17. v17's P1-A/B/C (adapter contract, DATA_SOURCES.md, Groq budget
governor) are merged into `main` and confirmed clean (lint + targeted tests
verified after merge). This revision is Phase 2 of `GRAND_PLAN.md` (§2):
narrative & story intelligence — the claim matrix and the graph layer that
lets a story from March link forward to one in September as the same ongoing
narrative.

**P2-A must land first — everything else depends on the schema.** P2-B, P2-C,
P2-D can proceed in parallel once P2-A is merged.

Every new table needs an idempotent migration in `supabase/migrations/`,
following the existing convention in
`supabase/migrations/20260925000000_globe_events_schema.sql` (`DO $$ ... IF
NOT EXISTS` for enum types, `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT
EXISTS`).

---

## P2-A — Schema: `claims`, `claim_evidence`, `entity_edges`, `topic_groups`

### Goal

The storage layer for "who says what" per story, and the graph layer for
linking stories across time. Postgres tables, not a separate graph DB —
matches the existing Supabase/pgvector setup, no new infra.

**Note on naming:** `FactCheckRecord` already exists in `src/schema/models.py`
and also has a field called `claim` — that table is for verdicts from
*external* fact-checkers (ClaimBuster, ClaimReview, manual). The new `claims`
table below is for atomic assertions *this pipeline* extracts from its own
units. They're related concepts but not the same table — don't merge them,
and don't rename either one. A future phase can cross-reference a `Claim` row
against `FactCheckRecord` by matching text/entities; out of scope here.

### Additions to `src/schema/models.py`

Add after `CorrectionRecord` (end of file):

```python
class ClaimType(str, PyEnum):
    FACT = "fact"
    ALLEGATION = "allegation"
    PREDICTION = "prediction"
    QUOTE = "quote"


class Claim(Base):
    """An atomic factual assertion extracted from a story's reporting units.

    Distinct from FactCheckRecord (verdicts from external fact-checkers) --
    this is the pipeline's own claim-extraction output, feeding the per-story
    claim matrix described in GRAND_PLAN.md §2.
    """
    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_story_id", "story_id"),
        Index("ix_claims_claim_type", "claim_type"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    story_id = Column(UUID(as_uuid=True), ForeignKey("stories.id", ondelete="CASCADE"), nullable=False)
    text = Column(Text, nullable=False)
    claim_type = Column(Enum(ClaimType), nullable=False, default=ClaimType.FACT)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    story = relationship("Story")
    evidence = relationship("ClaimEvidence", back_populates="claim", cascade="all, delete-orphan")


class ClaimStance(str, PyEnum):
    SUPPORTS = "supports"
    DISPUTES = "disputes"
    NEUTRAL = "neutral"


class ClaimEvidence(Base):
    """Edge: which reporting unit said what about which claim.

    This is the "source A says X, source B says Y" matrix -- one row per
    (claim, unit) pair.
    """
    __tablename__ = "claim_evidence"
    __table_args__ = (
        UniqueConstraint("claim_id", "unit_id", name="uq_claim_unit"),
        Index("ix_claim_evidence_unit_id", "unit_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    claim_id = Column(UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False)
    unit_id = Column(UUID(as_uuid=True), ForeignKey("reporting_units.id", ondelete="CASCADE"), nullable=False)
    stance = Column(Enum(ClaimStance), nullable=False, default=ClaimStance.NEUTRAL)
    confidence = Column(Integer, nullable=False, default=50)  # 0-100, matches FactCheckRecord's convention
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    claim = relationship("Claim", back_populates="evidence")
    unit = relationship("ReportingUnit")


class EdgePredicate(str, PyEnum):
    CORROBORATES = "corroborates"
    DISPUTES = "disputes"
    SAME_EVENT_AS = "same_event_as"
    PART_OF_NARRATIVE = "part_of_narrative"
    CAUSED_BY = "caused_by"


class EntityEdge(Base):
    """Generic typed edge between two things in the graph.

    Polymorphic on purpose (subject_type/object_type + a UUID, no FK
    constraint on subject_id/object_id) so this one table can hold
    story-to-story narrative links now and claim-to-claim or entity-to-entity
    edges later without a schema change. Phase 2 (P2-D) only populates
    subject_type="story"/object_type="story" edges -- validate the type
    strings at the application layer, not the database layer.
    """
    __tablename__ = "entity_edges"
    __table_args__ = (
        Index("ix_entity_edges_subject", "subject_type", "subject_id"),
        Index("ix_entity_edges_object", "object_type", "object_id"),
        Index("ix_entity_edges_predicate", "predicate"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    subject_type = Column(String(20), nullable=False)  # "story" for now; "claim"/"entity" later
    subject_id = Column(UUID(as_uuid=True), nullable=False)
    predicate = Column(Enum(EdgePredicate), nullable=False)
    object_type = Column(String(20), nullable=False)
    object_id = Column(UUID(as_uuid=True), nullable=False)
    confidence = Column(Integer, nullable=False, default=50)  # 0-100
    source_unit_id = Column(UUID(as_uuid=True), ForeignKey("reporting_units.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class TopicGroup(Base):
    """Hierarchical topic group, e.g. Geopolitics -> Middle East -> Israel-Gaza."""
    __tablename__ = "topic_groups"
    __table_args__ = (
        UniqueConstraint("name", "parent_group_id", name="uq_topic_group_name_parent"),
        Index("ix_topic_groups_parent", "parent_group_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    parent_group_id = Column(UUID(as_uuid=True), ForeignKey("topic_groups.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    children = relationship("TopicGroup", backref=backref("parent", remote_side=[id]))


class StoryTopicGroup(Base):
    """Many-to-many: stories <-> topic_groups, with an assignment confidence."""
    __tablename__ = "story_topic_groups"
    __table_args__ = (UniqueConstraint("story_id", "topic_group_id", name="uq_story_topic_group"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    story_id = Column(UUID(as_uuid=True), ForeignKey("stories.id", ondelete="CASCADE"), nullable=False)
    topic_group_id = Column(UUID(as_uuid=True), ForeignKey("topic_groups.id", ondelete="CASCADE"), nullable=False)
    confidence = Column(Integer, nullable=True)  # 0-100, null if manually assigned
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
```

`backref` needs adding to the existing `from sqlalchemy.orm import declarative_base, relationship` import line at the top of `models.py` -- change to `from sqlalchemy.orm import declarative_base, relationship, backref`.

### New migration: `supabase/migrations/20260926000000_phase2_claims_and_graph.sql`

```sql
-- Phase 2: claims, claim evidence, entity edges, topic groups
-- Idempotent migration -- see GRAND_PLAN.md §2

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'claim_type') THEN
        CREATE TYPE claim_type AS ENUM ('fact', 'allegation', 'prediction', 'quote');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'claim_stance') THEN
        CREATE TYPE claim_stance AS ENUM ('supports', 'disputes', 'neutral');
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'edge_predicate') THEN
        CREATE TYPE edge_predicate AS ENUM (
            'corroborates', 'disputes', 'same_event_as', 'part_of_narrative', 'caused_by'
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS claims (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    claim_type claim_type NOT NULL DEFAULT 'fact',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    id SERIAL PRIMARY KEY,
    claim_id UUID NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    unit_id UUID NOT NULL REFERENCES reporting_units(id) ON DELETE CASCADE,
    stance claim_stance NOT NULL DEFAULT 'neutral',
    confidence INTEGER NOT NULL DEFAULT 50,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_claim_unit UNIQUE (claim_id, unit_id)
);

CREATE TABLE IF NOT EXISTS entity_edges (
    id SERIAL PRIMARY KEY,
    subject_type VARCHAR(20) NOT NULL,
    subject_id UUID NOT NULL,
    predicate edge_predicate NOT NULL,
    object_type VARCHAR(20) NOT NULL,
    object_id UUID NOT NULL,
    confidence INTEGER NOT NULL DEFAULT 50,
    source_unit_id UUID REFERENCES reporting_units(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS topic_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    parent_group_id UUID REFERENCES topic_groups(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_topic_group_name_parent UNIQUE (name, parent_group_id)
);

CREATE TABLE IF NOT EXISTS story_topic_groups (
    id SERIAL PRIMARY KEY,
    story_id UUID NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    topic_group_id UUID NOT NULL REFERENCES topic_groups(id) ON DELETE CASCADE,
    confidence INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_story_topic_group UNIQUE (story_id, topic_group_id)
);

CREATE INDEX IF NOT EXISTS ix_claims_story_id ON claims(story_id);
CREATE INDEX IF NOT EXISTS ix_claims_claim_type ON claims(claim_type);
CREATE INDEX IF NOT EXISTS ix_claim_evidence_unit_id ON claim_evidence(unit_id);
CREATE INDEX IF NOT EXISTS ix_entity_edges_subject ON entity_edges(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS ix_entity_edges_object ON entity_edges(object_type, object_id);
CREATE INDEX IF NOT EXISTS ix_entity_edges_predicate ON entity_edges(predicate);
CREATE INDEX IF NOT EXISTS ix_topic_groups_parent ON topic_groups(parent_group_id);
```

### Acceptance

`scripts/check_schema.py` (if it diffs models against live schema) passes, or
if it doesn't cover new tables, confirm manually: run the migration against a
throwaway/local Postgres, then confirm `Base.metadata.create_all` in
`init_db()` doesn't try to recreate tables that already exist (it shouldn't --
SQLAlchemy's `create_all` is already idempotent against existing tables, this
is just confirming the migration and the ORM models agree on column names and
types). No existing table's columns are touched by this migration.

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

1. P2-A first, always — nothing else compiles without the schema.
2. P2-B, P2-C, P2-D in parallel after P2-A merges.
3. Run the full pytest suite after each task. Land as separate, revertable
   PRs, same as Phase 1.
4. Before starting, read `src/enrichment/pipeline.py`'s `enrich_recent_stories`
   and `src/verification/stories.py`'s `build_stories` in full — both are
   referenced above as "reuse this pattern" and neither is reproduced in full
   here; guessing their structure instead of reading it is how Phase 1's
   mock-patching bug happened.