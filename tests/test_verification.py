"""Tests for verification pipeline: units, stories, and tiers integration."""

import pytest
import pytest_asyncio
from datetime import datetime, timezone
import uuid
from sqlalchemy import select, text

from src.verification.units import get_owner_group, build_reporting_units
from src.verification.stories import build_stories
from src.verification.tiers import apply_tier1_gate, evaluate_tier1_gate
from src.shared.database import init_db, get_session
from src.schema.models import (
    RawArticle, ReportingUnit, Story, StoryUnitLink, SourceTier
)


class TestIsLocalhostDb:
    """Tests for the _is_localhost_db pure function."""

    def test_localhost_urls(self):
        assert _is_localhost_db("postgresql://user:pass@localhost:5432/db") is True
        assert _is_localhost_db("postgresql+asyncpg://user:pass@127.0.0.1:5432/db") is True
        assert _is_localhost_db("postgresql://user:pass@localhost/db") is True
        assert _is_localhost_db("postgresql://localhost/db") is True

    def test_non_localhost_urls(self):
        assert _is_localhost_db("postgresql://user:pass@db.example.com:5432/db") is False
        assert _is_localhost_db("postgresql://user:pass@192.168.1.1:5432/db") is False
        assert _is_localhost_db("") is False

    def test_malformed_urls(self):
        assert _is_localhost_db("not-a-url") is False
        assert _is_localhost_db("postgresql://") is False


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

def _is_localhost_db(url: str) -> bool:
    """Check if a database URL points to localhost/127.0.0.1."""
    if not url:
        return False
    # Check for localhost or 127.0.0.1 in the host portion
    # Handle formats: postgresql://user:pass@host:port/db, postgresql+asyncpg://...
    try:
        # Extract host from URL
        if "@" in url:
            host_part = url.split("@")[1].split("/")[0]
        else:
            host_part = url.split("://")[1].split("/")[0]
        host = host_part.split(":")[0]
        return host in ("localhost", "127.0.0.1")
    except Exception:
        return False


_TRUNCATE_ALL = (
    "TRUNCATE TABLE status_log, story_unit_links, stories, reporting_units, "
    "raw_articles, entity_aliases, canonical_entities RESTART IDENTITY CASCADE"
)


@pytest_asyncio.fixture
async def db_session():
    """Create a test database session and clean up after."""
    from src.shared.config import get_settings
    settings = get_settings()

    # SAFETY GUARD: Only run TRUNCATE on localhost databases
    if not _is_localhost_db(settings.database_url):
        pytest.skip(f"Refusing to run integration tests against non-localhost DB: {settings.database_url}")

    await init_db()

    async with get_session() as session:
        # TRUNCATE CASCADE handles the circular FK between raw_articles and reporting_units.
        # Canonical entity tables are included because build_stories() creates rows in them.
        await session.execute(text(_TRUNCATE_ALL))
        await session.commit()

        yield session

        await session.execute(text(_TRUNCATE_ALL))
        await session.commit()


async def _make_unit(session, *, domain, owner, entities, tier=SourceTier.TIER1):
    """Create a RawArticle plus the ReportingUnit that represents it.

    build_stories() reads entities from the unit's representative RawArticle, so the
    article has to exist (representative_article_id is a real FK).
    """
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


# Entity sets are chosen so Jaccard on canonical IDs is 0.5 (>= the 0.4 attach threshold)
# between the "White House" and "Capitol" articles, and 0 against the London/economy one.
ENT_WHITE_HOUSE = {"PERSON": ["John Smith"], "GPE": ["Washington"], "ORG": ["White House"]}
ENT_CAPITOL = {"PERSON": ["John Smith"], "GPE": ["Washington"], "ORG": ["Capitol"]}
ENT_ECONOMY = {"PERSON": ["Jane Doe"], "GPE": ["London"], "ORG": ["Bank of England"]}


@pytest.mark.asyncio
async def test_build_reporting_units_integration(db_session):
    """Integration test: build_reporting_units creates units from raw articles."""
    now = datetime.now(timezone.utc)
    ap_body = (
        "A major event happened today in the capital city. "
        "Officials confirmed the details of the event to reporters."
    )

    def article(url, url_hash, title, body, domain, entities):
        return RawArticle(
            id=uuid.uuid4(),
            url=url,
            url_hash=url_hash,
            title=title,
            body_text=body,
            source_domain=domain,
            source_tier=SourceTier.TIER1,
            published_at=now,
            entities=entities,
        )

    # Two AP articles with the SAME body. Clustering compares 5-word shingles at a 0.9
    # containment threshold, so "near-duplicates" that are merely reworded do not cluster.
    article1 = article("https://apnews.com/article/test-1", "hash1",
                       "Breaking: Major Event Happens", ap_body, "apnews.com", ENT_WHITE_HOUSE)
    article2 = article("https://apnews.com/article/test-2", "hash2",
                       "Breaking: Major Event Happens (updated)", ap_body, "apnews.com", ENT_WHITE_HOUSE)
    article3 = article("https://reuters.com/article/test-3", "hash3",
                       "Reuters Reports on Different Topic",
                       "Reuters reports on a completely different story about the economy.",
                       "reuters.com", ENT_ECONOMY)

    for art in [article1, article2, article3]:
        db_session.add(art)
    await db_session.commit()

    units_created = await build_reporting_units(db_session)

    # 2 units: one cluster (apnews x2) and one single (reuters)
    assert units_created == 2

    result = await db_session.execute(select(ReportingUnit))
    units = result.scalars().all()
    assert len(units) == 2

    ap_unit = next(u for u in units if u.tier1_owner_groups.get("AP", 0) > 0)
    assert ap_unit.article_count == 2
    assert ap_unit.source_tiers == {"tier1": 2}
    assert ap_unit.tier1_owner_groups == {"AP": 2}

    reuters_unit = next(u for u in units if u.tier1_owner_groups.get("Reuters", 0) > 0)
    assert reuters_unit.article_count == 1
    assert reuters_unit.source_tiers == {"tier1": 1}
    assert reuters_unit.tier1_owner_groups == {"Reuters": 1}


@pytest.mark.asyncio
async def test_build_stories_groups_by_entities(db_session):
    """Integration test: build_stories groups units by canonical-entity Jaccard."""
    await _make_unit(db_session, domain="apnews.com", owner="AP", entities=ENT_WHITE_HOUSE)
    await _make_unit(db_session, domain="reuters.com", owner="Reuters", entities=ENT_CAPITOL)
    await _make_unit(db_session, domain="bbc.com", owner="BBC", entities=ENT_ECONOMY)
    await db_session.commit()

    modified_story_ids = await build_stories(db_session)

    # 2 stories: unit1+unit2 (Jaccard 0.5), and unit3 on its own
    assert len(modified_story_ids) == 2

    result = await db_session.execute(select(Story))
    stories = result.scalars().all()
    assert len(stories) == 2

    link_counts = []
    for story in stories:
        result = await db_session.execute(
            select(StoryUnitLink).where(StoryUnitLink.story_id == story.id)
        )
        link_counts.append(len(result.scalars().all()))
    assert sorted(link_counts) == [1, 2]


@pytest.mark.asyncio
async def test_tier1_gate_passes_with_two_tier1_distinct_owners(db_session):
    """Integration test: tier-1 gate passes with 2 tier-1 units from different owners."""
    await _make_unit(db_session, domain="apnews.com", owner="AP", entities=ENT_WHITE_HOUSE)
    await _make_unit(db_session, domain="reuters.com", owner="Reuters", entities=ENT_CAPITOL)
    await db_session.commit()

    modified_story_ids = await build_stories(db_session)
    result = await apply_tier1_gate(db_session, story_ids=modified_story_ids)

    assert result["queued"] == 1
    assert result["blocked"] == 0

    result_stmt = await db_session.execute(select(Story).where(Story.id.in_(modified_story_ids)))
    for story in result_stmt.scalars().all():
        # A passing story stays PENDING (awaiting curator approval); it is not auto-QUEUED.
        assert story.status == Story.Status.PENDING
        assert story.tier1_unit_count == 2
        assert story.distinct_owners == 2


@pytest.mark.asyncio
async def test_tier1_gate_blocks_single_tier1_owner(db_session):
    """Integration test: tier-1 gate blocks when only 1 distinct tier-1 owner."""
    await _make_unit(db_session, domain="apnews.com", owner="AP", entities=ENT_WHITE_HOUSE)
    await _make_unit(db_session, domain="apnews.com", owner="AP", entities=ENT_CAPITOL)
    await db_session.commit()

    modified_story_ids = await build_stories(db_session)
    result = await apply_tier1_gate(db_session, story_ids=modified_story_ids)

    assert result["queued"] == 0
    assert result["blocked"] == 1

    result_stmt = await db_session.execute(select(Story).where(Story.id.in_(modified_story_ids)))
    for story in result_stmt.scalars().all():
        assert story.status == Story.Status.BLOCKED


@pytest.mark.asyncio
async def test_cross_run_story_attachment(db_session):
    """Integration test: build_stories attaches new units to recent BLOCKED stories."""
    # First run: one tier-1 unit -> BLOCKED story
    await _make_unit(db_session, domain="apnews.com", owner="AP", entities=ENT_WHITE_HOUSE)
    await db_session.commit()

    modified1 = await build_stories(db_session)
    result1 = await apply_tier1_gate(db_session, story_ids=modified1)
    assert result1["blocked"] == 1
    blocked_story_id = modified1[0]

    # Second run: a tier-1 unit from a different owner with similar entities
    await _make_unit(db_session, domain="reuters.com", owner="Reuters", entities=ENT_CAPITOL)
    await db_session.commit()

    modified2 = await build_stories(db_session)

    # Attaches to the existing story instead of creating a new one
    assert len(modified2) == 1
    assert modified2[0] == blocked_story_id

    result2 = await apply_tier1_gate(db_session, story_ids=modified2)
    assert result2["queued"] == 1
    assert result2["blocked"] == 0

    result = await db_session.execute(
        select(StoryUnitLink).where(StoryUnitLink.story_id == blocked_story_id)
    )
    assert len(result.scalars().all()) == 2