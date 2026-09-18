"""Source tier definitions and tier-1 gate logic."""

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import (
    ReportingUnit, Story, StoryUnitLink, SourceTier, Story
)
from src.shared.config import get_settings
from datetime import datetime, timezone
from collections import defaultdict


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


async def apply_tier1_gate(session: AsyncSession) -> dict:
    """
    Apply the tier-1 gate: stories need ≥2 tier-1 reporting units
    from distinct ownership groups.
    """
    settings = get_settings()
    min_units = settings.min_reporting_units_per_story

    # Get pending stories
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

        # Count tier-1 units by distinct owner groups
        tier1_owners = set()
        tier1_count = 0
        tier2_count = 0

        for unit in units:
            tiers = unit.source_tiers or {}
            if tiers.get("tier1", 0) > 0:
                tier1_count += tiers["tier1"]
                # Add distinct owners from TIER-1 articles only
                for owner, count in (unit.tier1_owner_groups or {}).items():
                    if count > 0:
                        tier1_owners.add(owner)

            tier2_count += tiers.get("tier2", 0)

        distinct_owners = len(tier1_owners)

        # Update story
        story.tier1_unit_count = tier1_count
        story.tier2_unit_count = tier2_count
        story.distinct_owners = distinct_owners

        if tier1_count >= min_units and distinct_owners >= 2:
            story.status = Story.Status.QUEUED
            story.gate_reason = f"Passed: {tier1_count} tier-1 units, {distinct_owners} distinct owners"
            queued += 1
        else:
            story.status = Story.Status.BLOCKED
            reasons = []
            if tier1_count < min_units:
                reasons.append(f"only {tier1_count} tier-1 units (need {min_units})")
            if distinct_owners < 2:
                reasons.append(f"only {distinct_owners} distinct tier-1 owners (need 2)")
            story.gate_reason = "Blocked: " + "; ".join(reasons)
            blocked += 1

    await session.commit()
    return {"queued": queued, "blocked": blocked}