"""Test for the stored vs computed tier1/owner counter mismatch bug."""
import pytest
import pytest_asyncio
from datetime import datetime, timezone
import uuid
from typing import AsyncGenerator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.verification.stories import build_stories
from src.verification.tiers import apply_tier1_gate
from src.schema.models import (
    RawArticle, ReportingUnit, Story, StoryUnitLink, SourceTier
)


async def _make_unit(session, *, domain, owner, entities, tier=SourceTier.TIER1):
    """Create a RawArticle plus the ReportingUnit that represents it."""
    now = datetime.now(timezone.utc)
    article = RawArticle(
        id=uuid.uuid4(),
        url=f"https://{domain}/article/{uuid.uuid4().hex[:8]}",
        url_hash=uuid.uuid4().hex,
        title=f"Test article from {domain}",
        body_text=f"Body text for a test article from {domain}",
        source_domain=domain,
        source_tier=tier,
        published_at=now,
        entities=entities,
    )
    session.add(article)
    await session.flush()

    unit = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=article.id,
        day=now.replace(hour=0, minute=0, second=0, microsecond=0),
        article_count=1,
        source_tiers={tier.value: 1},
        owner_groups={owner: 1},
        tier1_owner_groups={owner: 1} if tier == SourceTier.TIER1 else {},
    )
    session.add(unit)
    await session.flush()
    return unit


# Entity sets chosen so Jaccard on canonical IDs >= 0.4 (attach threshold)
ENT_WHITE_HOUSE = {"PERSON": ["John Smith"], "GPE": ["Washington"], "ORG": ["White House"]}
ENT_CAPITOL = {"PERSON": ["John Smith"], "GPE": ["Washington"], "ORG": ["Capitol"]}
ENT_ECONOMY = {"PERSON": ["Jane Doe"], "GPE": ["London"], "ORG": ["Bank of England"]}


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Use the test database session from conftest.py"""
    async_session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


@pytest.mark.asyncio
async def test_detach_unit_recomputes_counters(db_session):
    """
    Reproduces the bug: create a story with 2 linked units from 2 tier-1 owners,
    detach one unit, assert the stored counts drop to match computed.

    This test SHOULD FAIL currently (stored counts stay inflated).
    """
    # Create 3 units from 3 different tier-1 owners with similar entities
    # so they all get grouped into one story
    await _make_unit(db_session, domain="apnews.com", owner="AP", entities=ENT_WHITE_HOUSE)
    await _make_unit(db_session, domain="reuters.com", owner="Reuters", entities=ENT_CAPITOL)
    unit3 = await _make_unit(db_session, domain="bbc.com", owner="BBC", entities=ENT_WHITE_HOUSE)
    await db_session.commit()

    # Build stories - all 3 should group together (Jaccard >= 0.4)
    modified_story_ids = await build_stories(db_session)
    assert len(modified_story_ids) == 1
    story_id = modified_story_ids[0]

    # Apply tier-1 gate - this writes the counters
    result = await apply_tier1_gate(db_session, story_ids=modified_story_ids)
    assert result["queued"] == 1  # 3 tier-1 units, 3 distinct owners -> passes

    # Verify stored counts match computed (3 tier-1, 3 owners)
    stmt = select(Story).where(Story.id == story_id)
    story_result = await db_session.execute(stmt)
    story = story_result.scalar_one()
    assert story.tier1_unit_count == 3
    assert story.distinct_owners == 3

    # Now DETACH one unit from the story (simulating re-canonicalization/merging)
    stmt = select(StoryUnitLink).where(
        StoryUnitLink.story_id == story_id,
        StoryUnitLink.unit_id == unit3.id
    )
    link_result = await db_session.execute(stmt)
    link = link_result.scalar_one()
    await db_session.delete(link)
    await db_session.commit()

    # NOW re-apply the gate - this should recompute from current links
    result = await apply_tier1_gate(db_session, story_ids=[story_id])
    # With 2 units from 2 owners, should still pass
    assert result["queued"] == 1

    # Check stored counters were UPDATED to match current links (2 tier-1, 2 owners)
    stmt = select(Story).where(Story.id == story_id)
    story_result = await db_session.execute(stmt)
    story = story_result.scalar_one()
    print(f"After detach: stored tier1={story.tier1_unit_count}, owners={story.distinct_owners}")

    # This assertion should PASS after fix, but FAILS before fix
    # (stored counts remain at 3 instead of dropping to 2)
    assert story.tier1_unit_count == 2, f"Expected tier1_count=2 after detach, got {story.tier1_unit_count}"
    assert story.distinct_owners == 2, f"Expected distinct_owners=2 after detach, got {story.distinct_owners}"


@pytest.mark.asyncio
async def test_attach_unit_recomputes_counters(db_session):
    """
    Test that attaching a unit to an existing story recomputes counters.
    """
    # First run: one tier-1 unit -> BLOCKED story
    await _make_unit(db_session, domain="apnews.com", owner="AP", entities=ENT_WHITE_HOUSE)
    await db_session.commit()

    modified1 = await build_stories(db_session)
    result1 = await apply_tier1_gate(db_session, story_ids=modified1)
    assert result1["blocked"] == 1
    story_id = modified1[0]

    # Verify initial state: 1 tier-1, 1 owner
    stmt = select(Story).where(Story.id == story_id)
    story_result = await db_session.execute(stmt)
    story = story_result.scalar_one()
    assert story.tier1_unit_count == 1
    assert story.distinct_owners == 1

    # Second run: a tier-1 unit from a different owner with similar entities
    await _make_unit(db_session, domain="reuters.com", owner="Reuters", entities=ENT_CAPITOL)
    await db_session.commit()

    modified2 = await build_stories(db_session)

    # Should attach to existing story
    assert len(modified2) == 1
    assert modified2[0] == story_id

    # Apply gate again - should recompute
    result2 = await apply_tier1_gate(db_session, story_ids=modified2)
    assert result2["queued"] == 1

    # Verify counters updated to 2 tier-1, 2 owners
    stmt = select(Story).where(Story.id == story_id)
    story_result = await db_session.execute(stmt)
    story = story_result.scalar_one()
    print(f"After attach: stored tier1={story.tier1_unit_count}, owners={story.distinct_owners}")

    assert story.tier1_unit_count == 2, f"Expected tier1_count=2 after attach, got {story.tier1_unit_count}"
    assert story.distinct_owners == 2, f"Expected distinct_owners=2 after attach, got {story.distinct_owners}"