"""Narrative arc linking: connect stories across time via entity overlap.

This module creates EntityEdge rows linking a story to earlier stories
covering the same ongoing situation, beyond the 48h story-grouping window.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import Story, EntityEdge, EdgePredicate
from src.shared.analyzer_versions import NARRATIVE_VERSION, compute_input_hash

logger = logging.getLogger(__name__)

# Thresholds for narrative arc linking
SAME_EVENT_AS_THRESHOLD = 0.5   # Jaccard >= 0.5 -> SAME_EVENT_AS
PART_OF_NARRATIVE_THRESHOLD = 0.2  # Jaccard >= 0.2 -> PART_OF_NARRATIVE
LOOKBACK_DAYS = 90  # Look back this many days for narrative arcs


def _entity_jaccard(set1: set[str], set2: set[str]) -> float:
    """Calculate Jaccard similarity between two entity sets."""
    if not set1 or not set2:
        return 0.0
    intersection = len(set1 & set2)
    union = len(set1 | set2)
    return intersection / union if union > 0 else 0.0


async def _get_stories_with_entities(
    session: AsyncSession,
    cutoff: datetime,
    exclude_story_id=None,
) -> list[tuple[uuid.UUID, set[str]]]:
    """Get gate-passed stories from lookback period with their primary_entities.

    Phase 2 (2026-10-03): widened from QUEUED-only to the same pool
    PHASE2_STATUSES covers, because an arc is a claim that two stories are
    about the same ongoing situation. A link between a PENDING story and a
    QUEUED one is true regardless of which side the curator has already
    approved, and restricting the peer set to QUEUED would make arcs
    impossible while QUEUED is still empty. This does not publish anything:
    EntityEdge rows are derived state, not public rows.
    """
    from src.verification.phase2 import PHASE2_STATUSES

    stmt = (
        select(Story)
        .where(Story.status.in_(PHASE2_STATUSES))
        .where(Story.created_at >= cutoff)
    )
    if exclude_story_id:
        stmt = stmt.where(Story.id != exclude_story_id)

    result = await session.execute(stmt)
    stories = result.scalars().all()

    story_entities = []
    for story in stories:
        entities = set(list(story.primary_entities or []))
        if entities:  # Only include stories with entities
            story_entities.append((story.id, entities))

    return story_entities


async def link_narrative_arcs(session: AsyncSession, story_id) -> list[dict]:
    """Link a newly-created or newly-gated story to earlier stories.

    For a given story, look back LOOKBACK_DAYS at other QUEUED stories
    sharing entities above threshold. Creates EntityEdge rows:
    - SAME_EVENT_AS for strong overlap (Jaccard >= 0.5)
    - PART_OF_NARRATIVE for weaker overlap (Jaccard >= 0.2)

    Returns list of created edges.
    """

    results = []

    # Get the target story
    stmt = select(Story).where(Story.id == story_id)
    result = await session.execute(stmt)
    story = result.scalar_one_or_none()
    if not story:
        return [{"story_id": str(story_id), "error": "Story not found"}]

    target_entities = set(list(story.primary_entities or []))
    if not target_entities:
        return results

    # Look back for older stories
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    older_stories = await _get_stories_with_entities(session, cutoff, exclude_story_id=story_id)

    for older_story_id, older_entities in older_stories:
        jaccard = _entity_jaccard(target_entities, older_entities)

        if jaccard >= SAME_EVENT_AS_THRESHOLD:
            predicate = EdgePredicate.SAME_EVENT_AS
            confidence = min(100, int(jaccard * 100))
        elif jaccard >= PART_OF_NARRATIVE_THRESHOLD:
            predicate = EdgePredicate.PART_OF_NARRATIVE
            confidence = min(100, int(jaccard * 100))
        else:
            continue  # No significant overlap

        # Check if edge already exists (idempotent)
        stmt = select(EntityEdge).where(
            EntityEdge.subject_type == "story",
            EntityEdge.subject_id == story_id,
            EntityEdge.predicate == predicate,
            EntityEdge.object_type == "story",
            EntityEdge.object_id == older_story_id,
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            results.append({
                "subject_id": str(story_id),
                "object_id": str(older_story_id),
                "predicate": predicate.value,
                "confidence": existing.confidence,
                "created": False,
            })
            continue

        # Create the edge (story -> older_story, so subject is newer, object is older)
        edge = EntityEdge(
            subject_type="story",
            subject_id=story_id,
            predicate=predicate,
            object_type="story",
            object_id=older_story_id,
            confidence=confidence,
            source_unit_id=None,  # Could link to a specific unit later
            # The business key of the edge is the 5-tuple (uq_entity_edge), so that is what
            # the hash covers -- not the confidence, which is a judgement recorded about the
            # edge rather than an input to deciding it exists.
            analyzer_version=NARRATIVE_VERSION,
            input_hash=compute_input_hash(
                NARRATIVE_VERSION,
                "story",
                str(story_id),
                predicate.value,
                "story",
                str(older_story_id),
            ),
        )
        session.add(edge)
        await session.flush()
        results.append({
            "subject_id": str(story_id),
            "object_id": str(older_story_id),
            "predicate": predicate.value,
            "confidence": confidence,
            "created": True,
        })

    # Only commit if we actually created new edges
    created_any = any(r["created"] for r in results)
    if created_any:
        await session.commit()

    return results


async def link_narrative_arcs_for_recent_stories(
    session_factory, hours_back: int = 168, max_stories: int = 100,
) -> list[dict]:
    """Batch narrative arc linking for recent gate-passed stories.

    Retargeted for Phase 2 (2026-10-03): the pool used to be QUEUED-only and
    selected nothing in practice, so entity_edges was empty. PENDING stories
    have passed the dynamic gate; public exposure stays human-gated.
    """
    from src.verification.phase2 import select_phase2_stories

    async with session_factory() as session:
        stories = await select_phase2_stories(
            session, hours_back=hours_back, max_stories=max_stories
        )

    story_ids = [s.id for s in stories]

    if not story_ids:
        logger.info("No gate-passed stories to link narrative arcs for")
        return []

    logger.info(
        f"Linking narrative arcs for {len(story_ids)} gate-passed stories"
    )

    results = []
    for story_id in story_ids:
        async with session_factory() as session:
            result = await link_narrative_arcs(session, story_id)
            results.extend(result)

    return results