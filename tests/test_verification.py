"""Tests for verification pipeline: units, stories, and tiers integration."""

import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
import uuid
from sqlalchemy import select, delete, text

from src.verification.units import get_owner_group, build_reporting_units
from src.verification.stories import build_stories
from src.verification.tiers import apply_tier1_gate, evaluate_tier1_gate
from src.shared.database import init_db, get_session, _get_engine
from src.schema.models import (
    RawArticle, ReportingUnit, Story, StoryUnitLink, SourceTier, StatusLog
)


class TestOwnershipGroups:
    def test_known_domains(self):
        assert get_owner_group("apnews.com") == "AP"
        assert get_owner_group("reuters.com") == "Reuters"
        assert get_owner_group("bbc.com") == "BBC"

    def test_subdomain_matching(self):
        assert get_owner_group("www.apnews.com") == "AP"
        assert get_owner_group("www.bbc.com") == "BBC"

    def test_unknown_domain(self):
        assert get_owner_group("randomblog.com") == "Independent"


class TestEvaluateTier1GatePure:
    """Tests for the pure evaluate_tier1_gate function."""

    def test_passes_with_two_tier1_different_owners(self):
        units = [
            ("tier1", "AP"),
            ("tier1", "Reuters"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True
        assert "Gate passed" in reason

    def test_fails_only_one_tier1_unit(self):
        units = [
            ("tier1", "AP"),
            ("tier2", "NYT"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 1 tier-1 units" in reason

    def test_fails_two_tier1_same_owner(self):
        units = [
            ("tier1", "AP"),
            ("tier1", "AP"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "owner" in reason.lower()

    def test_fails_no_tier1_units(self):
        units = [
            ("tier2", "NYT"),
            ("tier2", "WaPo"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 0 tier-1 units" in reason


# Integration tests using PostgreSQL (pgvector service container)
# These require DATABASE_URL to be set (e.g., postgresql+asyncpg://postgres:postgres@localhost:5432/test_news)

def _has_database():
    """Check if database is available for integration tests."""
    import os
    return bool(os.getenv("DATABASE_URL"))


@pytest_asyncio.fixture
async def db_session():
    """Create a test database session and clean up after."""
    # Initialize database
    await init_db()

    async with get_session() as session:
        # Clean up any existing test data - use TRUNCATE CASCADE to handle circular FKs
        # RawArticle.reporting_unit_id -> ReportingUnit.id (nullable)
        # ReportingUnit.representative_article_id -> RawArticle.id (NOT NULL)
        # These form a circular dependency, so TRUNCATE CASCADE is needed
        await session.execute(text("TRUNCATE TABLE status_log, story_unit_links, stories, reporting_units, raw_articles RESTART IDENTITY CASCADE"))
        await session.commit()

        yield session

        # Cleanup after test - same approach
        await session.execute(text("TRUNCATE TABLE status_log, story_unit_links, stories, reporting_units, raw_articles RESTART IDENTITY CASCADE"))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_database(), reason="Requires PostgreSQL database (DATABASE_URL)")
async def test_build_reporting_units_integration(db_session):
    """Integration test: build_reporting_units creates units from raw articles."""
    # Create test articles - two near-duplicates from same domain
    now = datetime.now(timezone.utc)

    article1 = RawArticle(
        id=uuid.uuid4(),
        url="https://apnews.com/article/test-1",
        url_hash="hash1",
        title="Breaking: Major Event Happens",
        body_text="A major event happened today in the capital city. Officials confirmed the details.",
        summary="Major event in capital city confirmed by officials.",
        source_domain="apnews.com",
        source_tier=SourceTier.TIER1,
        published_at=now,
        entities={"PERSON": ["John Smith"], "ORG": ["AP"], "GPE": ["Washington"]},
    )

    article2 = RawArticle(
        id=uuid.uuid4(),
        url="https://apnews.com/article/test-2",
        url_hash="hash2",
        title="Breaking: Major Event Occurs",
        body_text="A major event occurred today in the capital. Authorities confirmed the information.",
        summary="Major event in capital confirmed by authorities.",
        source_domain="apnews.com",
        source_tier=SourceTier.TIER1,
        published_at=now,
        entities={"PERSON": ["John Smith"], "ORG": ["AP"], "GPE": ["Washington"]},
    )

    # Different domain article (should not cluster with above)
    article3 = RawArticle(
        id=uuid.uuid4(),
        url="https://reuters.com/article/test-3",
        url_hash="hash3",
        title="Reuters Reports on Different Topic",
        body_text="Reuters reports on a completely different story about economy.",
        summary="Economy story from Reuters.",
        source_domain="reuters.com",
        source_tier=SourceTier.TIER1,
        published_at=now,
        entities={"PERSON": ["Jane Doe"], "ORG": ["Reuters"], "GPE": ["London"]},
    )

    for art in [article1, article2, article3]:
        db_session.add(art)
    await db_session.commit()

    # Build reporting units
    units_created = await build_reporting_units(db_session)

    # Should create 2 units: one cluster (apnews x2) and one single (reuters)
    assert units_created == 2

    # Verify units were created correctly
    stmt = select(ReportingUnit)
    result = await db_session.execute(stmt)
    units = result.scalars().all()

    assert len(units) == 2

    # Check that the AP cluster has 2 articles (owner group "AP")
    ap_unit = next(u for u in units if u.tier1_owner_groups.get("AP", 0) > 0)
    assert ap_unit.article_count == 2
    assert ap_unit.source_tiers == {"tier1": 2}
    assert ap_unit.tier1_owner_groups == {"AP": 2}

    # Check that the Reuters unit has 1 article (owner group "Reuters")
    reuters_unit = next(u for u in units if u.tier1_owner_groups.get("Reuters", 0) > 0)
    assert reuters_unit.article_count == 1
    assert reuters_unit.source_tiers == {"tier1": 1}
    assert reuters_unit.tier1_owner_groups == {"Reuters": 1}


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_database(), reason="Requires PostgreSQL database (DATABASE_URL)")
async def test_build_stories_groups_by_entities(db_session):
    """Integration test: build_stories groups units by entity Jaccard."""
    now = datetime.now(timezone.utc)
    day = now.date()

    # Create reporting units directly (bypassing clustering)
    unit1 = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=uuid.uuid4(),
        day=day,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
                article_count=1,
    )

    unit2 = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=uuid.uuid4(),
        day=day,
        source_tiers={"tier1": 1},
        owner_groups={"Reuters": 1},
        tier1_owner_groups={"Reuters": 1},
        primary_entities={"John Smith", "Washington", "Capitol"},  # Overlapping entities
        article_count=1,
    )

    # Different topic
    unit3 = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=uuid.uuid4(),
        day=day,
        source_tiers={"tier1": 1},
        owner_groups={"BBC": 1},
        tier1_owner_groups={"BBC": 1},
        primary_entities={"London", "Economy", "Bank of England"},
        article_count=1,
    )

    for u in [unit1, unit2, unit3]:
        db_session.add(u)
    await db_session.commit()

    # Build stories
    modified_story_ids = await build_stories(db_session)

    # Should create 2 stories: one with unit1+unit2 (Jaccard ~0.5), one with unit3
    assert len(modified_story_ids) == 2

    # Verify stories
    stmt = select(Story)
    result = await db_session.execute(stmt)
    stories = result.scalars().all()

    assert len(stories) == 2

    # One story should have 2 units (the clustered ones)
    story_with_2 = next(s for s in stories if len(s.primary_entities or []) > 2)
    assert story_with_2 is not None

    # Verify links
    for story in stories:
        stmt = select(StoryUnitLink).where(StoryUnitLink.story_id == story.id)
        result = await db_session.execute(stmt)
        links = result.scalars().all()
        if story.id == story_with_2.id:
            assert len(links) == 2
        else:
            assert len(links) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_database(), reason="Requires PostgreSQL database (DATABASE_URL)")
async def test_tier1_gate_passes_with_two_tier1_distinct_owners(db_session):
    """Integration test: tier-1 gate passes with 2 tier-1 units from different owners."""
    now = datetime.now(timezone.utc)
    day = now.date()

    # Create two tier-1 units from different owners
    unit1 = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=uuid.uuid4(),
        day=day,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
                article_count=1,
    )

    unit2 = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=uuid.uuid4(),
        day=day,
        source_tiers={"tier1": 1},
        owner_groups={"Reuters": 1},
        tier1_owner_groups={"Reuters": 1},
        primary_entities={"John Smith", "Washington", "Capitol"},
        article_count=1,
    )

    for u in [unit1, unit2]:
        db_session.add(u)
    await db_session.commit()

    # Build stories
    modified_story_ids = await build_stories(db_session)

    # Apply tier-1 gate
    result = await apply_tier1_gate(db_session, story_ids=modified_story_ids)

    # Should queue 1 story (2 tier-1 units from different owners)
    assert result["queued"] == 1
    assert result["blocked"] == 0

    # Verify story status
    stmt = select(Story).where(Story.id.in_(modified_story_ids))
    result_stmt = await db_session.execute(stmt)
    stories = result_stmt.scalars().all()

    for story in stories:
        assert story.status == Story.Status.QUEUED
        assert story.tier1_unit_count == 2
        assert story.distinct_owners == 2


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_database(), reason="Requires PostgreSQL database (DATABASE_URL)")
async def test_tier1_gate_blocks_single_tier1_owner(db_session):
    """Integration test: tier-1 gate blocks when only 1 distinct tier-1 owner."""
    now = datetime.now(timezone.utc)
    day = now.date()

    # Create two tier-1 units from SAME owner
    unit1 = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=uuid.uuid4(),
        day=day,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
                article_count=1,
    )

    unit2 = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=uuid.uuid4(),
        day=day,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
        primary_entities={"John Smith", "Washington", "Capitol"},
        article_count=1,
    )

    for u in [unit1, unit2]:
        db_session.add(u)
    await db_session.commit()

    # Build stories
    modified_story_ids = await build_stories(db_session)

    # Apply tier-1 gate
    result = await apply_tier1_gate(db_session, story_ids=modified_story_ids)

    # Should block (only 1 distinct owner)
    assert result["queued"] == 0
    assert result["blocked"] == 1

    # Verify story status
    stmt = select(Story).where(Story.id.in_(modified_story_ids))
    result_stmt = await db_session.execute(stmt)
    stories = result_stmt.scalars().all()

    for story in stories:
        assert story.status == Story.Status.BLOCKED


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_database(), reason="Requires PostgreSQL database (DATABASE_URL)")
async def test_cross_run_story_attachment(db_session):
    """Integration test: build_stories attaches new units to recent BLOCKED stories."""
    now = datetime.now(timezone.utc)
    day = now.date()

    # First run: create a BLOCKED story with one tier-1 unit
    unit1 = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=uuid.uuid4(),
        day=day,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
                article_count=1,
    )
    db_session.add(unit1)
    await db_session.commit()

    # Build story (will be BLOCKED - only 1 tier-1 unit)
    modified1 = await build_stories(db_session)
    result1 = await apply_tier1_gate(db_session, story_ids=modified1)
    assert result1["blocked"] == 1

    # Get the blocked story ID
    blocked_story_id = modified1[0]

    # Second run: add a second tier-1 unit from different owner with similar entities
    unit2 = ReportingUnit(
        id=uuid.uuid4(),
        representative_article_id=uuid.uuid4(),
        day=day,
        source_tiers={"tier1": 1},
        owner_groups={"Reuters": 1},
        tier1_owner_groups={"Reuters": 1},
        primary_entities={"John Smith", "Washington", "Capitol"},  # Similar entities
        article_count=1,
    )
    db_session.add(unit2)
    await db_session.commit()

    # Build stories - should attach to existing BLOCKED story
    modified2 = await build_stories(db_session)

    # Should modify the same story (not create new)
    assert len(modified2) == 1
    assert modified2[0] == blocked_story_id

    # Apply tier-1 gate - should now pass
    result2 = await apply_tier1_gate(db_session, story_ids=modified2)
    assert result2["queued"] == 1
    assert result2["blocked"] == 0

    # Verify story now has 2 units
    stmt = select(StoryUnitLink).where(StoryUnitLink.story_id == blocked_story_id)
    result = await db_session.execute(stmt)
    links = result.scalars().all()
    assert len(links) == 2