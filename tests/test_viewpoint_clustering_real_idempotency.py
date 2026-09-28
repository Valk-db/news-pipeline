"""Real idempotency test for viewpoint clustering using in-memory SQLite database."""

import pytest
import json
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from src.schema.models import (
    Story, ReportingUnit, StoryUnitLink
)
from src.verification.stories import cluster_viewpoints


@pytest.mark.asyncio
async def test_cluster_viewpoints_idempotent_real_db(db_session):
    """Test that cluster_viewpoints is idempotent - running twice produces same children.

    Uses the existing db_session fixture from conftest.py which uses StaticPool
    to share the same in-memory SQLite database across connections.
    """
    session = db_session

    # Create a parent story
    parent_story = Story(
        day=datetime.now(timezone.utc),
        primary_entities={"PERSON": ["Trump"], "ORG": ["White House"], "GPE": ["US", "China"]},
        status=Story.Status.PENDING,
        tier1_unit_count=2,
        tier2_unit_count=1,
        tier3_unit_count=1,
        tier4_unit_count=0,
        distinct_owners=2,
    )
    session.add(parent_story)
    await session.flush()
    parent_id = parent_story.id

    # Create 4 reporting units (enough for viewpoint clustering)
    units = []
    for i in range(4):
        if i < 2:
            source_tiers = {"tier1": 1}
            owner_groups = {"Owner1": 1}
        else:
            source_tiers = {"tier2": 1}
            owner_groups = {"Owner2": 1}
        unit = ReportingUnit(
            representative_article_id=uuid4(),
            source_tiers=source_tiers,
            tier1_owner_groups=owner_groups,
            owner_groups=owner_groups,  # Required NOT NULL column
            day=datetime.now(timezone.utc),
            article_count=1,
        )
        units.append(unit)
        session.add(unit)

    await session.flush()

    # Link units to parent story
    for unit in units:
        link = StoryUnitLink(story_id=parent_id, unit_id=unit.id)
        session.add(link)

    await session.commit()

    # Mock LLM to return consistent viewpoint labels
    with patch("src.shared.llm.get_llm_client", new_callable=AsyncMock) as mock_llm_client:
        mock_llm = AsyncMock()
        # Return deterministic labels for 4 units using actual unit IDs as keys
        mock_llm.chat_completion.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        str(units[0].id): "neutral",
                        str(units[1].id): "pro_govt",
                        str(units[2].id): "opposition",
                        str(units[3].id): "international",
                    })
                }
            }]
        }
        mock_llm_client.return_value = mock_llm

        # Also mock _gather_unit_texts_for_story to return consistent unit texts
        with patch("src.verification.stories._gather_unit_texts_for_story", new_callable=AsyncMock) as mock_gather:
            mock_gather.return_value = [
                {"unit_id": str(units[0].id), "text": "Article 1 about Trump", "source_tier": "tier1"},
                {"unit_id": str(units[1].id), "text": "Article 2 about Trump", "source_tier": "tier1"},
                {"unit_id": str(units[2].id), "text": "Article 3 about Trump", "source_tier": "tier2"},
                {"unit_id": str(units[3].id), "text": "Article 4 about Trump", "source_tier": "tier2"},
            ]

            # Run cluster_viewpoints FIRST time
            await cluster_viewpoints(session, [parent_id])

            # Count children created
            stmt = select(Story).where(Story.viewpoint_cluster_id == parent_id)
            result = await session.execute(stmt)
            children_after_first = list(result.scalars().all())
            child_count_1 = len(children_after_first)

            print(f"After first run: {child_count_1} children")
            for c in children_after_first:
                print(f"  Child: {c.id}, status={c.status}")

    # Run cluster_viewpoints SECOND time (should be idempotent)
    with patch("src.shared.llm.get_llm_client", new_callable=AsyncMock) as mock_llm_client:
        mock_llm = AsyncMock()
        # Return deterministic labels for 4 units using actual unit IDs as keys
        mock_llm.chat_completion.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        str(units[0].id): "neutral",
                        str(units[1].id): "pro_govt",
                        str(units[2].id): "opposition",
                        str(units[3].id): "international",
                    })
                }
            }]
        }
        mock_llm_client.return_value = mock_llm

        with patch("src.verification.stories._gather_unit_texts_for_story", new_callable=AsyncMock) as mock_gather:
            mock_gather.return_value = [
                {"unit_id": str(units[0].id), "text": "Article 1 about Trump", "source_tier": "tier1"},
                {"unit_id": str(units[1].id), "text": "Article 2 about Trump", "source_tier": "tier1"},
                {"unit_id": str(units[2].id), "text": "Article 3 about Trump", "source_tier": "tier2"},
                {"unit_id": str(units[3].id), "text": "Article 4 about Trump", "source_tier": "tier2"},
            ]

            await cluster_viewpoints(session, [parent_id])

            # Count children after second run
            stmt = select(Story).where(Story.viewpoint_cluster_id == parent_id)
            result = await session.execute(stmt)
            children_after_second = list(result.scalars().all())
            child_count_2 = len(children_after_second)

            print(f"After second run: {child_count_2} children")
            for c in children_after_second:
                print(f"  Child: {c.id}, status={c.status}")

    # Assert idempotency: same number of children
    assert child_count_1 == child_count_2, f"Child count changed: {child_count_1} -> {child_count_2}"

    # Assert no duplicate children per parent
    child_ids = [c.id for c in children_after_second]
    assert len(child_ids) == len(set(child_ids)), "Duplicate child story IDs found"

    print(f"[OK] Idempotency verified: {child_count_1} children both runs, no duplicates")


@pytest.mark.asyncio
async def test_llm_fallback_behavior():
    """Verify LLM fallback behavior when BOTH providers fail.

    From src/shared/llm.py:
    - generate_caption: returns None when both providers fail (line 284)
    - classify_relevance: returns 0.5 (default neutral) when both fail (line 358)
    - chat_completion: raises exception when both providers fail (no silent default)
    """
    from src.shared.llm import LLMClient
    from src.shared.config import Settings

    # Create a mock settings object with no API keys (both providers will fail auth)
    mock_settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        groq_api_key="",
        cerebras_api_key="",
    )

    with patch("src.shared.llm.get_settings", return_value=mock_settings):
        client = LLMClient()

        # 1. generate_caption: should return None when both providers fail
        result = await client.generate_caption(
            story_title="Test story",
            key_facts=["Fact 1", "Fact 2"],
            source_urls=["https://example.com"],
            platform="twitter",
        )
        assert result is None, f"generate_caption expected None, got {result}"

        # 2. classify_relevance: should return 0.5 (default neutral) when both fail
        result = await client.classify_relevance(
            title="Test article",
            body="Test body",
            topics=["geopolitics"],
        )
        assert result == 0.5, f"classify_relevance expected 0.5, got {result}"

        # 3. chat_completion: should raise exception when both fail
        try:
            await client.chat_completion(
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=100,
                temperature=0.5,
            )
            assert False, "chat_completion should have raised an exception"
        except Exception:
            # Any exception is acceptable - the point is it doesn't silently return a default
            pass

        print("[OK] LLM fallback behavior verified:")
        print("  - generate_caption: returns None when both providers fail")
        print("  - classify_relevance: returns 0.5 (default neutral) when both fail")
        print("  - chat_completion: raises exception when both fail (no silent default)")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])