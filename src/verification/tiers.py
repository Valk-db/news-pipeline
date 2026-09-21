"""Source tier definitions and tier-1 gate logic."""

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import (
    ReportingUnit, Story, StoryUnitLink, SourceTier
)
from typing import List, Optional, Tuple


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

    for story in stories:
        # Get linked reporting units
        stmt = (
            select(ReportingUnit)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .where(StoryUnitLink.story_id == story.id)
        )
        result = await session.execute(stmt)
        units = result.scalars().all()

        # Convert real ReportingUnit rows to (tier, owner_group) tuples for pure function
        # tier1_owner_groups is already a JSON dict like {"AP": 1, "BBC": 2} computed in units.py
        tier_owner_pairs = []
        tier1_total = 0
        tier2_total = 0
        tier1_owners = set()

        for u in units:
            # source_tiers is JSON like {"tier1": 2, "tier2": 1}
            source_tiers = u.source_tiers or {}
            for tier, count in source_tiers.items():
                # Use tier1_owner_groups for tier1 owners (already correct ownership mapping)
                # For tier2, we don't need owners for the gate
                if tier == "tier1":
                    tier1_owner_groups = u.tier1_owner_groups or {}
                    for owner, owner_count in tier1_owner_groups.items():
                        for _ in range(owner_count):
                            tier_owner_pairs.append((tier, owner))
                    tier1_total += count
                    tier1_owners.update(tier1_owner_groups.keys())
                elif tier == "tier2":
                    tier2_total += count

        # Use pure function for gate decision
        should_queue, reason = evaluate_tier1_gate(tier_owner_pairs)

        # Count for storage
        tier1_count = tier1_total
        tier2_count = tier2_total
        distinct_owners = len(tier1_owners)

        # Update story
        story.tier1_unit_count = tier1_count
        story.tier2_unit_count = tier2_count
        story.distinct_owners = distinct_owners

        if should_queue:
            # Gate passes: keep PENDING (awaiting curator approval)
            story.status = Story.Status.PENDING
            story.gate_reason = f"Passed gate: {tier1_count} tier-1 units, {distinct_owners} distinct owners"
            queued += 1
        else:
            story.status = Story.Status.BLOCKED
            story.gate_reason = f"Blocked: {reason}"
            blocked += 1

    await session.commit()
    return {"queued": queued, "blocked": blocked}