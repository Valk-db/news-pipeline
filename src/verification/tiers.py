"""Source tier definitions and tier-1 gate logic."""

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import (
    ReportingUnit, Story, StoryUnitLink, SourceTier
)
from src.verification.units import get_owner_group
from src.shared.config import get_settings
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


def evaluate_tier1_gate(units: List[ReportingUnit]) -> Tuple[bool, str]:
    """
    Pure function to evaluate if a story passes the tier-1 gate.

    Gate passes iff:
    - At least 2 units with source_tier == TIER1
    - Those units have ≥2 distinct ownership groups

    Returns (should_queue, reason).
    """
    from src.verification.units import get_owner_group

    tier1_units = [u for u in units if u.source_tier == SourceTier.TIER1]
    if len(tier1_units) < 2:
        return False, f"Only {len(tier1_units)} tier-1 units (need ≥2)"

    owners = {get_owner_group(u.source_domain) for u in tier1_units}
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

    If story_ids provided, only evaluate those stories (including BLOCKED).
    Otherwise evaluate all PENDING stories.
    """
    # Build query - if story_ids provided, get those (any status), else get PENDING
    if story_ids:
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

        # Use pure function for gate decision
        should_queue, reason = evaluate_tier1_gate(list(units))

        # Count for storage
        tier1_count = sum(1 for u in units if u.source_tier == SourceTier.TIER1)
        tier2_count = sum(1 for u in units if u.source_tier == SourceTier.TIER2)
        distinct_owners = len({get_owner_group(u.source_domain) for u in units if u.source_tier == SourceTier.TIER1})

        # Update story
        story.tier1_unit_count = tier1_count
        story.tier2_unit_count = tier2_count
        story.distinct_owners = distinct_owners

        if should_queue:
            story.status = Story.Status.QUEUED
            story.gate_reason = f"Passed: {tier1_count} tier-1 units, {distinct_owners} distinct owners"
            queued += 1
        else:
            story.status = Story.Status.BLOCKED
            story.gate_reason = f"Blocked: {reason}"
            blocked += 1

    await session.commit()
    return {"queued": queued, "blocked": blocked}