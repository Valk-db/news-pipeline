"""Source tier definitions and tier-1 gate logic."""

import uuid
from collections import defaultdict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import (
    ReportingUnit, Story, StoryUnitLink, SourceTier
)
from typing import List, Optional, Tuple


async def recompute_story_counters(session: AsyncSession, story_ids: List[uuid.UUID]) -> None:
    """
    Recompute story tier1_unit_count, tier2_unit_count, and distinct_owners
    from current StoryUnitLinks.

    This should be called whenever StoryUnitLinks change (attach/detach/merge).

    Note: distinct_owners is computed from tier1_owner_groups to match
    the tier-1 gate logic which requires >=2 distinct tier-1 owners.
    """
    if not story_ids:
        return

    # Get all linked units for these stories with their tier and tier1_owner_groups
    stmt = (
        select(StoryUnitLink.story_id, ReportingUnit.source_tiers, ReportingUnit.tier1_owner_groups)
        .join(ReportingUnit, ReportingUnit.id == StoryUnitLink.unit_id)
        .where(StoryUnitLink.story_id.in_(story_ids))
    )
    result = await session.execute(stmt)
    rows = result.all()

    # Aggregate by story_id
    story_tiers: dict[uuid.UUID, dict[str, int]] = {}
    story_tier1_owners: dict[uuid.UUID, set[str]] = {}

    for story_id, source_tiers, tier1_owner_groups in rows:
        if story_id not in story_tiers:
            story_tiers[story_id] = {}
            story_tier1_owners[story_id] = set()

        # Sum up tier counts
        for tier_str, count in (source_tiers or {}).items():
            story_tiers[story_id][tier_str] = story_tiers[story_id].get(tier_str, 0) + count

        # Collect tier-1 owners (owners that have tier-1 units)
        for owner, _ in (tier1_owner_groups or {}).items():
            story_tier1_owners[story_id].add(owner)

    # Now compute tier1_unit_count, tier2_unit_count, and distinct_owners for each story
    # Batch fetch all stories in one query
    stmt_stories = select(Story).where(Story.id.in_(story_ids))
    result_stories = await session.execute(stmt_stories)
    stories = {s.id: s for s in result_stories.scalars().all()}

    for story_id in story_ids:
        tiers = story_tiers.get(story_id, {})
        tier1_owners = story_tier1_owners.get(story_id, set())

        tier1_count = tiers.get("tier1", 0)
        tier2_count = tiers.get("tier2", 0)
        distinct_owners = len(tier1_owners)  # distinct tier-1 owners

        story = stories.get(story_id)
        if story:
            story.tier1_unit_count = tier1_count
            story.tier2_unit_count = tier2_count
            story.distinct_owners = distinct_owners

    await session.flush()


# Tier-1 sources (verified editorial standards)
TIER1_DOMAINS = {
    "bbc.com",
    "theguardian.com",
    "npr.org",
    "apnews.com",
    "reuters.com",
}

# Tier-2 sources (reputable but not wire-service level)
TIER2_DOMAINS = {
    "nytimes.com",
    "washingtonpost.com",
    "wsj.com",
    "ft.com",
    "economist.com",
    "foreignpolicy.com",
    "foreignaffairs.com",
    "csis.org",
    "brookings.edu",
    "chathamhouse.org",
    "un.org",
    "who.int",
}


def classify_source_tier(domain: str) -> SourceTier:
    """Classify a domain into a source tier."""
    if domain in TIER1_DOMAINS:
        return SourceTier.TIER1
    if domain in TIER2_DOMAINS:
        return SourceTier.TIER2
    return SourceTier.TIER3


def evaluate_tier1_gate(tier_owner_pairs: List[tuple[str, str]]) -> Tuple[bool, str]:
    """
    Pure function to evaluate if a story passes the tier-1 gate.

    Gate passes iff:
    - At least 2 units with source_tier == TIER1
    - Those units have ≥2 distinct ownership groups

    Args:
        tier_owner_pairs: List of (source_tier, owner_group) tuples extracted from ReportingUnit rows.
                          source_tier values: "tier1", "tier2", "tier3" (strings from JSON)
                          owner_group values: ownership group names (strings from JSON)

    Returns (should_queue, reason).
    """
    tier1_pairs = [(tier, owner) for tier, owner in tier_owner_pairs if tier == "tier1"]
    if len(tier1_pairs) < 2:
        return False, f"Only {len(tier1_pairs)} tier-1 units (need ≥2)"

    owners = {owner for _, owner in tier1_pairs}
    if len(owners) < 2:
        return False, f"Tier-1 units from only {len(owners)} owner(s) (need ≥2 distinct)"

    return True, "Gate passed"


async def apply_tier1_gate(
    session: AsyncSession,
    story_ids: Optional[List[uuid.UUID]] = None
) -> dict:
    """
    Apply the tier-1 gate: stories need ≥2 tier-1 reporting units
    from distinct ownership groups.

    If story_ids provided (not None), only evaluate those stories (including BLOCKED).
    Otherwise evaluate all PENDING stories.
    """
    # Build query - if story_ids is not None, get those (any status), else get PENDING
    if story_ids is not None:
        stmt = select(Story).where(Story.id.in_(story_ids))
    else:
        stmt = select(Story).where(Story.status == Story.Status.PENDING)
    result = await session.execute(stmt)
    stories = result.scalars().all()

    queued = 0
    blocked = 0

    # First, recompute all counters from current StoryUnitLinks
    story_id_list = [s.id for s in stories]
    if story_id_list:
        await recompute_story_counters(session, story_id_list)

    # Batch fetch all units for all stories in a single query
    story_units: defaultdict[uuid.UUID, list] = defaultdict(list)
    if story_id_list:
        stmt = (
            select(Story, ReportingUnit)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .join(Story, Story.id == StoryUnitLink.story_id)
            .where(Story.id.in_(story_id_list))
        )
        result = await session.execute(stmt)
        for story, unit in result.all():
            story_units[story.id].append(unit)

    for story in stories:
        # Get linked reporting units for gate evaluation (from batched query)
        units = story_units.get(story.id, [])

        # Convert real ReportingUnit rows to (tier, owner_group) tuples for pure function
        # tier1_owner_groups is already a JSON dict like {"AP": 1, "BBC": 2} computed in units.py
        tier_owner_pairs = []

        for u in units:
            # source_tiers is JSON like {"tier1": 2, "tier2": 1}
            source_tiers = u.source_tiers or {}
            for tier, count in source_tiers.items():
                if tier == "tier1":
                    tier1_owner_groups = u.tier1_owner_groups or {}
                    for owner, owner_count in tier1_owner_groups.items():
                        for _ in range(owner_count):
                            tier_owner_pairs.append((tier, owner))
                # tier2 doesn't matter for the gate

        # Use pure function for gate decision
        should_queue, reason = evaluate_tier1_gate(tier_owner_pairs)

        # Update story status and gate_reason (counters already updated by recompute_story_counters)
        if should_queue:
            # Gate passes: keep PENDING (awaiting curator approval)
            story.status = Story.Status.PENDING
            story.gate_reason = f"Passed gate: {story.tier1_unit_count} tier-1 units, {story.distinct_owners} distinct owners"
            queued += 1
        else:
            story.status = Story.Status.BLOCKED
            story.gate_reason = f"Blocked: {reason}"
            blocked += 1

    await session.commit()
    return {"queued": queued, "blocked": blocked}