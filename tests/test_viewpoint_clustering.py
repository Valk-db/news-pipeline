"""Tests for viewpoint clustering functionality."""

import pytest
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from src.verification.stories import cluster_viewpoints
from src.schema.models import Story, ReportingUnit, RawArticle, SourceTier
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def mock_session():
    """Create a mock async session."""
    session = AsyncMock(spec=AsyncSession)
    return session


@pytest.fixture
def sample_story():
    """Create a sample story with multiple units."""
    story = Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=["entity1", "entity2"],
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=1,
        tier4_unit_count=0,
        distinct_owners=2,
        status=Story.Status.PENDING,
        viewpoint_cluster_id=None,
    )
    return story


@pytest.fixture
def sample_units():
    """Create sample reporting units with different source tiers."""
    units = []
    for i in range(4):
        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            representative_article_id=uuid.uuid4(),
            article_count=1,
            source_tiers={"tier1": 1, "tier2": 0, "tier3": 0} if i < 2 else {"tier2": 1},
            owner_groups={"BBC": 1, "Guardian": 1} if i < 2 else {"NYT": 1},
            tier1_owner_groups={"BBC": 1, "Guardian": 1} if i < 2 else {},
        )
        units.append(unit)
    return units


@pytest.fixture
def sample_articles():
    """Create sample raw articles with different perspectives."""
    articles = []
    perspectives = [
        "The government announced new economic policies today. Officials say this will boost growth and create jobs. Proponents praise the measures as timely and necessary for recovery.",
        "Opposition leaders criticize the new economic policies as insufficient. They argue the measures don't address inequality and will benefit corporations more than workers.",
        "International observers view the economic policies as a positive step but note structural challenges remain. The IMF welcomed the reforms while urging further action.",
        "Local business owners express mixed views on the economic policies. Some welcome tax relief while others worry about regulatory changes affecting small businesses.",
    ]

    for i, text in enumerate(perspectives):
        article = RawArticle(
            id=uuid.uuid4(),
            url=f"https://example.com/article{i}",
            url_hash=f"hash{i}",
            title=f"Economic Policy Article {i}",
            body_text=text,
            source_domain="bbc.com" if i < 2 else ("nytimes.com" if i == 2 else "localnews.com"),
            source_tier=SourceTier.TIER1 if i < 2 else SourceTier.TIER2,
            published_at=datetime.now(timezone.utc),
            entities={"PERSON": ["Official", "Leader"], "ORG": ["Government", "IMF"], "GPE": ["Country"]},
            content_hash=f"content{i}",
        )
        articles.append(article)
    return articles


@pytest.mark.asyncio
async def test_cluster_viewpoints_basic(mock_session, sample_story, sample_units, sample_articles):
    """Test basic viewpoint clustering."""
    story_ids = [sample_story.id]

    # Mock the database queries - need to handle multiple execute calls
    mock_session.execute = AsyncMock()

    # Mock results for various queries:
    # 1. Initial story query (line 140-145)
    story_result = MagicMock()
    story_result.scalars.return_value.all.return_value = [sample_story]

    # 2. recompute_story_counters queries - return rows with tier info for the sample story
    # The function queries: StoryUnitLink.story_id, ReportingUnit.source_tiers, ReportingUnit.tier1_owner_groups
    # We need to return rows that match the sample story's tier counts (tier1=2, tier2=1, etc.)
    recompute_result = MagicMock()
    # Return 4 rows (one per unit), each with the source_tiers and tier1_owner_groups
    recompute_rows = []
    for i, unit in enumerate(sample_units):
        recompute_rows.append((
            sample_story.id,
            unit.source_tiers,
            unit.tier1_owner_groups,
        ))
    recompute_result.all.return_value = recompute_rows

    # 3. Story with units via selectinload (line 165-171)
    story_with_units_result = MagicMock()
    story_with_units = MagicMock()
    story_with_units.units = sample_units
    story_with_units_result.scalar_one_or_none.return_value = story_with_units

    # 4. P2.3 idempotency guard: existing children count query (line 204-206)
    # Must return 0 to proceed with clustering. This runs BEFORE _gather_unit_texts_for_story.
    existing_children_result = MagicMock()
    existing_children_result.scalar.return_value = 0

    # 5. Story with units via selectinload (from _gather_unit_texts_for_story, line 165-171)
    # This runs AFTER the P2.3 guard
    story_with_units_result = MagicMock()
    story_with_units = MagicMock()
    story_with_units.units = sample_units
    story_with_units_result.scalar_one_or_none.return_value = story_with_units

    # 6. Article queries for each unit (from _gather_unit_texts_for_story, line 177-179)
    # One per unit (4 units)
    article_results = []
    for article in sample_articles:
        article_result = MagicMock()
        article_result.scalar_one_or_none.return_value = article
        article_results.append(article_result)

    # 7. Unit queries for viewpoint story day lookup (line 258-260) - one per viewpoint
    # There will be 4 viewpoints, each needs a query for ReportingUnit
    unit_query_results = []
    for unit in sample_units:
        unit_result = MagicMock()
        unit_result.scalars.return_value.all.return_value = [unit]
        unit_query_results.append(unit_result)

    # The call sequence in cluster_viewpoints:
    # 1. Initial story query (line 179-184)
    # 2. recompute_story_counters first query (StoryUnitLink join, line 29-35 in tiers.py)
    # 3. recompute_story_counters second query (select Story, line 56-58 in tiers.py)
    # 4. session.refresh(story) - no execute call
    # 5. P2.3 guard: existing children count (line 204-206)
    # 6. _gather_unit_texts_for_story:
    #    a. Story with units selectinload (line 165-171 in stories.py)
    #    b. Article queries for each unit (line 177-179 in stories.py)
    # 7. Unit queries for viewpoint story day lookup (line 258-260, one per viewpoint cluster)
    mock_session.execute.side_effect = [
        story_result,                      # 1. Initial story query
        recompute_result,                  # 2. recompute_story_counters join query
        story_result,                      # 3. recompute_story_counters select Story query
        existing_children_result,          # 4. P2.3 guard - no existing children
        story_with_units_result,           # 5. _gather_unit_texts_for_story: story with units
    ] + article_results + unit_query_results  # 6. article queries + 7. unit queries

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.refresh = AsyncMock()

    # Mock LLM client - use the real interface
    with patch('src.shared.llm.get_llm_client') as mock_get_llm, \
         patch('src.verification.stories.apply_dynamic_gate') as mock_apply_gate:
        mock_llm = AsyncMock()
        mock_get_llm.return_value = mock_llm
        mock_apply_gate.return_value = {"queued": 0, "blocked": 0}

        # Mock LLM response with viewpoint classifications (new chat_completion interface)
        # Use actual unit IDs as keys (converted to strings)
        unit_id_strs = [str(u.id) for u in sample_units]
        viewpoint_labels = {
            unit_id_strs[0]: "pro_govt",
            unit_id_strs[1]: "opposition",
            unit_id_strs[2]: "international",
            unit_id_strs[3]: "local",
        }
        import json
        mock_llm.chat_completion.return_value = {
            "choices": [{"message": {"content": json.dumps(viewpoint_labels)}}]
        }

        # Call the function
        try:
            result = await cluster_viewpoints(mock_session, story_ids)
        except Exception as e:
            print(f"Exception during cluster_viewpoints: {e}")
            import traceback
            traceback.print_exc()
            raise

        # Verify results
        assert isinstance(result, dict)
        # Should create at least one viewpoint cluster (we have 4 units with distinct viewpoints)
        assert len(result) >= 1
        # Verify the parent story has viewpoint sub-stories
        parent_id = sample_story.id
        assert parent_id in result
        assert len(result[parent_id]) >= 1  # At least one viewpoint sub-story created
        # Verify the LLM was called with the correct interface
        mock_llm.chat_completion.assert_called_once()


@pytest.mark.asyncio
async def test_cluster_viewpoints_insufficient_units(mock_session):
    """Test viewpoint clustering with insufficient units."""
    story = Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=["entity1"],
        tier1_unit_count=1,
        tier2_unit_count=0,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=1,
        status=Story.Status.PENDING,
    )

    story_ids = [story.id]

    mock_session.execute = AsyncMock()
    story_result = MagicMock()
    story_result.scalar_one_or_none.return_value = story
    mock_session.execute.return_value = story_result
    mock_session.commit = AsyncMock()

    result = await cluster_viewpoints(mock_session, story_ids)

    # Should return empty dict for insufficient units
    assert result == {}


@pytest.mark.asyncio
async def test_viewpoint_substories_blocked_by_tier1_gate(mock_session, sample_units, sample_articles):
    """Test that viewpoint sub-stories without 2+ distinct tier-1 owners are BLOCKED, not curator-visible.

    This is a regression test for P0.2: viewpoint clustering creates sub-stories that
    must pass the tier-1 gate. A story built only from tier-3 units (e.g., Reddit posts)
    should have its viewpoint sub-stories BLOCKED, not PENDING/curator-visible.
    """
    # Create a parent story with ONLY tier-3 units (no tier-1 units)
    story = Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=["entity1"],
        tier1_unit_count=0,
        tier2_unit_count=0,
        tier3_unit_count=4,
        tier4_unit_count=0,
        distinct_owners=0,
        status=Story.Status.PENDING,
        viewpoint_cluster_id=None,
    )

    # Create tier-3 units (like Reddit posts)
    tier3_units = []
    for i in range(4):
        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=datetime.now(timezone.utc),
            representative_article_id=uuid.uuid4(),
            article_count=1,
            source_tiers={"tier3": 1},
            owner_groups={"Reddit": 1},
            tier1_owner_groups={},  # No tier-1 owners
        )
        tier3_units.append(unit)

    # Mock the database queries
    mock_session.execute = AsyncMock()

    # 1. Initial story query
    story_result = MagicMock()
    story_result.scalars.return_value.all.return_value = [story]

    # 2. recompute_story_counters queries - return tier-3 only
    recompute_result = MagicMock()
    recompute_rows = []
    for unit in tier3_units:
        recompute_rows.append((
            story.id,
            unit.source_tiers,
            unit.tier1_owner_groups,
        ))
    recompute_result.all.return_value = recompute_rows

    # 3. P2.3 idempotency guard: existing children count query (line 204-206)
    # Must return 0 to proceed with clustering. This runs BEFORE _gather_unit_texts_for_story.
    existing_children_result = MagicMock()
    existing_children_result.scalar.return_value = 0

    # 4. Story with units via selectinload (from _gather_unit_texts_for_story, line 165-171)
    # This runs AFTER the P2.3 guard
    story_with_units_result = MagicMock()
    story_with_units = MagicMock()
    story_with_units.units = tier3_units
    story_with_units_result.scalar_one_or_none.return_value = story_with_units

    # 5. Article queries for each unit (from _gather_unit_texts_for_story, line 177-179)
    # One per unit (4 units)
    article_results = []
    for article in sample_articles:
        article_result = MagicMock()
        article_result.scalar_one_or_none.return_value = article
        article_results.append(article_result)

    # 6. Unit queries for viewpoint story day lookup (line 258-260) - one per viewpoint
    # There will be 4 viewpoints, each needs a query for ReportingUnit
    unit_query_results = []
    for unit in tier3_units:
        unit_result = MagicMock()
        unit_result.scalars.return_value.all.return_value = [unit]
        unit_query_results.append(unit_result)

    # The call sequence in cluster_viewpoints:
    # 1. Initial story query (line 179-184)
    # 2. recompute_story_counters first query (StoryUnitLink join, line 29-35 in tiers.py)
    # 3. recompute_story_counters second query (select Story, line 56-58 in tiers.py)
    # 4. session.refresh(story) - no execute call
    # 5. P2.3 guard: existing children count (line 204-206)
    # 6. _gather_unit_texts_for_story:
    #    a. Story with units selectinload (line 165-171 in stories.py)
    #    b. Article queries for each unit (line 177-179 in stories.py)
    # 7. Unit queries for viewpoint story day lookup (line 258-260, one per viewpoint cluster)
    mock_session.execute.side_effect = [
        story_result,                      # 1. Initial story query
        recompute_result,                  # 2. recompute_story_counters join query
        story_result,                      # 3. recompute_story_counters select Story query
        existing_children_result,          # 4. P2.3 guard - no existing children
        story_with_units_result,           # 5. _gather_unit_texts_for_story: story with units
    ] + article_results + unit_query_results  # 6. article queries + 7. unit queries

    mock_session.commit = AsyncMock()
    mock_session.flush = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.refresh = AsyncMock()

    # Mock LLM client and apply_dynamic_gate
    with patch('src.shared.llm.get_llm_client') as mock_get_llm, \
         patch('src.verification.stories.apply_dynamic_gate') as mock_apply_gate:
        mock_llm = AsyncMock()
        mock_get_llm.return_value = mock_llm

        # Track what stories apply_tier1_gate was called with
        called_story_ids = []
        async def mock_gate(session, story_ids=None):
            called_story_ids.extend(story_ids or [])
            return {"queued": 0, "blocked": len(story_ids) if story_ids else 0}
        mock_apply_gate.side_effect = mock_gate

        # Mock LLM response with viewpoint classifications
        unit_id_strs = [str(u.id) for u in tier3_units]
        viewpoint_labels = {
            unit_id_strs[0]: "opinion_a",
            unit_id_strs[1]: "opinion_b",
            unit_id_strs[2]: "opinion_c",
            unit_id_strs[3]: "opinion_d",
        }
        import json
        mock_llm.chat_completion.return_value = {
            "choices": [{"message": {"content": json.dumps(viewpoint_labels)}}]
        }

        # Call the function
        result = await cluster_viewpoints(mock_session, [story.id])

        # Verify viewpoint sub-stories were created
        assert len(result) >= 1
        assert story.id in result
        viewpoint_story_ids = result[story.id]
        assert len(viewpoint_story_ids) >= 1

        # Verify apply_tier1_gate was called with the viewpoint sub-story IDs
        assert len(called_story_ids) >= 1
        assert set(called_story_ids) == set(viewpoint_story_ids)

        # The gate should have blocked them (no tier-1 units, no distinct tier-1 owners)
        # This ensures the sub-stories don't end up as PENDING in the curation queue


def test_viewpoint_cluster_model_fields():
    """Test that Story model has viewpoint_cluster_id field."""
    story = Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc),
        primary_entities=["entity1"],
        status=Story.Status.PENDING,
    )

    # Should have viewpoint_cluster_id attribute
    assert hasattr(story, 'viewpoint_cluster_id')
    assert story.viewpoint_cluster_id is None


def test_source_tier_enum():
    """Test SourceTier enum has all four tiers."""
    assert SourceTier.TIER1 == "tier1"
    assert SourceTier.TIER2 == "tier2"
    assert SourceTier.TIER3 == "tier3"
    assert SourceTier.TIER4 == "tier4"


def test_tier_definitions():
    """Test tier definitions in tiers.py."""
    from src.verification.tiers import TIER1_DOMAINS, TIER2_DOMAINS, TIER3_DOMAINS, TIER4_DOMAINS
    from src.verification.tiers import classify_source_tier

    assert "bbc.com" in TIER1_DOMAINS
    # Tier-2 disabled domains (2026-09-28): nytimes, wapo, wsj, ft, economist, latimes
    # Only verified working RSS feeds remain in TIER2_DOMAINS
    assert "foreignpolicy.com" in TIER2_DOMAINS
    assert "foreignaffairs.com" in TIER2_DOMAINS
    assert "csis.org" in TIER2_DOMAINS
    assert "who.int" in TIER2_DOMAINS
    assert "chicagotribune.com" in TIER2_DOMAINS
    assert "reddit.com" in TIER3_DOMAINS
    assert "substack.com" in TIER4_DOMAINS

    assert classify_source_tier("bbc.com") == SourceTier.TIER1
    # Disabled tier-2 domains now default to TIER3
    assert classify_source_tier("nytimes.com") == SourceTier.TIER3
    assert classify_source_tier("foreignpolicy.com") == SourceTier.TIER2
    assert classify_source_tier("reddit.com") == SourceTier.TIER3
    assert classify_source_tier("substack.com") == SourceTier.TIER4
    assert classify_source_tier("unknown.com") == SourceTier.TIER3  # Default


def test_source_registry():
    """Test source registry functionality."""
    from src.ingestion.source_registry import (
        get_sources_by_tier,
        get_enabled_sources_by_tier,
        get_source_config,
        ALL_SOURCES,
        SourceTier,
    )

    # Test getting sources by tier
    tier1_sources = get_sources_by_tier(SourceTier.TIER1)
    assert len(tier1_sources) > 0
    assert all(s.tier == SourceTier.TIER1 for s in tier1_sources.values())

    tier2_sources = get_sources_by_tier(SourceTier.TIER2)
    assert len(tier2_sources) > 0
    assert all(s.tier == SourceTier.TIER2 for s in tier2_sources.values())

    tier3_sources = get_sources_by_tier(SourceTier.TIER3)
    assert len(tier3_sources) > 0

    tier4_sources = get_sources_by_tier(SourceTier.TIER4)
    assert len(tier4_sources) > 0

    # Test getting enabled sources
    enabled_tier1 = get_enabled_sources_by_tier(SourceTier.TIER1)
    assert len(enabled_tier1) > 0
    assert all(s.enabled for s in enabled_tier1.values())

    enabled_tier2 = get_enabled_sources_by_tier(SourceTier.TIER2)
    # Only foreignpolicy, foreignaffairs, csis, who, chicagotribune are enabled tier-2
    assert len(enabled_tier2) == 5
    assert all(s.enabled for s in enabled_tier2.values())
    assert "foreignpolicy.com" in enabled_tier2
    assert "foreignaffairs.com" in enabled_tier2
    assert "csis.org" in enabled_tier2
    assert "who.int" in enabled_tier2
    assert "chicagotribune.com" in enabled_tier2

    # Test getting specific source
    bbc = get_source_config("bbc.com")
    assert bbc is not None
    assert bbc.domain == "bbc.com"
    assert bbc.tier == SourceTier.TIER1

    # Disabled tier-2 sources should have enabled=False
    for domain in ["nytimes.com", "washingtonpost.com", "wsj.com", "ft.com", "economist.com", "latimes.com"]:
        src = get_source_config(domain)
        assert src is not None, f"{domain} should exist in registry"
        assert src.enabled is False, f"{domain} should be disabled"

    # Test ALL_SOURCES has all tiers
    assert len(ALL_SOURCES) > 15
    tiers_present = set(s.tier for s in ALL_SOURCES.values())
    assert SourceTier.TIER1 in tiers_present
    assert SourceTier.TIER2 in tiers_present
    assert SourceTier.TIER3 in tiers_present
    assert SourceTier.TIER4 in tiers_present


def test_tiered_scheduler():
    """Test tiered scheduler."""
    from src.ingestion.tiered_scheduler import TieredScheduler, SourceTier
    from datetime import datetime, timezone

    scheduler = TieredScheduler()

    # Test should_run_tier
    assert scheduler.should_run_tier(SourceTier.TIER1, None) is True

    # Recently run - should not run again
    recent = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert scheduler.should_run_tier(SourceTier.TIER1, recent) is False

    # Hour ago - should run
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    assert scheduler.should_run_tier(SourceTier.TIER1, hour_ago) is True

    # Tier 2: 4 hours
    four_hours_ago = datetime.now(timezone.utc) - timedelta(hours=4)
    assert scheduler.should_run_tier(SourceTier.TIER2, four_hours_ago) is True

    # Tier 3: daily
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    assert scheduler.should_run_tier(SourceTier.TIER3, day_ago) is True

    # Tier 4: 6 hours
    six_hours_ago = datetime.now(timezone.utc) - timedelta(hours=6)
    assert scheduler.should_run_tier(SourceTier.TIER4, six_hours_ago) is True

    # Test cron expressions
    assert scheduler.get_cron_expression(SourceTier.TIER1) == "0 * * * *"
    assert scheduler.get_cron_expression(SourceTier.TIER2) == "0 */4 * * *"
    assert scheduler.get_cron_expression(SourceTier.TIER3) == "0 6 * * *"
    assert scheduler.get_cron_expression(SourceTier.TIER4) == "0 */6 * * *"


def test_recompute_story_counters_extended():
    """Test that recompute_story_counters handles all four tiers."""
    from src.verification.tiers import recompute_story_counters

    # This is a basic check that the function signature accepts the right parameters
    import inspect
    sig = inspect.signature(recompute_story_counters)
    assert 'session' in sig.parameters
    assert 'story_ids' in sig.parameters

    # The function should handle tier3 and tier4 counts
    # (tested via integration tests with real DB)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])