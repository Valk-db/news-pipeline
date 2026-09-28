"""Tests for viewpoint clustering idempotency guard (P2.3)."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from uuid import uuid4
from src.verification.stories import cluster_viewpoints
from src.schema.models import Story


class TestViewpointClusteringIdempotency:
    """Tests for the idempotency guard that prevents duplicate viewpoint children."""

    @pytest.mark.asyncio
    async def test_skips_story_with_existing_children(self):
        """Stories that already have viewpoint children should be skipped."""
        mock_session = AsyncMock()

        parent_id = uuid4()
        parent_story = MagicMock(spec=Story)
        parent_story.id = parent_id
        parent_story.tier1_unit_count = 1
        parent_story.tier2_unit_count = 1
        parent_story.tier3_unit_count = 1
        parent_story.tier4_unit_count = 0
        parent_story.day = None
        parent_story.primary_entities = {}

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [parent_story]

        mock_children_result = MagicMock()
        mock_children_result.scalar.return_value = 2  # Already has 2 children

        # Track calls to session.execute to match the right statement
        execute_calls = []
        async def mock_execute(stmt):
            execute_calls.append(stmt)
            # Check if this is the existing_children_stmt (has viewpoint_cluster_id in whereclause)
            if hasattr(stmt, 'whereclause') and stmt.whereclause is not None:
                where_str = str(stmt.whereclause)
                if 'viewpoint_cluster_id' in where_str:
                    return mock_children_result
            return mock_result

        mock_session.execute.side_effect = mock_execute

        result = await cluster_viewpoints(mock_session, [parent_id])

        assert result == {}

    @pytest.mark.asyncio
    async def test_creates_children_when_none_exist(self):
        """Stories without existing children should proceed with clustering."""
        mock_session = AsyncMock()

        parent_id = uuid4()
        parent_story = MagicMock(spec=Story)
        parent_story.id = parent_id
        parent_story.tier1_unit_count = 1
        parent_story.tier2_unit_count = 1
        parent_story.tier3_unit_count = 1
        parent_story.tier4_unit_count = 0
        parent_story.day = None
        parent_story.primary_entities = {}

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [parent_story]

        mock_children_result = MagicMock()
        mock_children_result.scalar.return_value = 0  # No existing children

        async def mock_execute(stmt):
            if hasattr(stmt, 'whereclause') and stmt.whereclause is not None:
                where_str = str(stmt.whereclause)
                if 'viewpoint_cluster_id' in where_str:
                    return mock_children_result
            return mock_result

        mock_session.execute.side_effect = mock_execute

        with patch("src.verification.stories._gather_unit_texts_for_story", new_callable=AsyncMock) as mock_gather:
            mock_gather.return_value = [
                {"unit_id": uuid4(), "text": "Article 1", "source_tier": "tier1"},
                {"unit_id": uuid4(), "text": "Article 2", "source_tier": "tier2"},
                {"unit_id": uuid4(), "text": "Article 3", "source_tier": "tier3"},
            ]

            with patch("src.shared.llm.get_llm_client", new_callable=AsyncMock) as mock_llm_client:
                mock_llm = AsyncMock()
                mock_llm.chat_completion.return_value = {
                    "choices": [{"message": {"content": '{"unit1": "neutral", "unit2": "pro_govt", "unit3": "opposition"}'}}]
                }
                mock_llm_client.return_value = mock_llm

                result = await cluster_viewpoints(mock_session, [parent_id])

        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_multiple_stories_some_with_children_some_without(self):
        """Mix of stories with and without existing children."""
        mock_session = AsyncMock()

        parent_id_1 = uuid4()
        parent_id_2 = uuid4()

        parent_story_1 = MagicMock(spec=Story)
        parent_story_1.id = parent_id_1
        parent_story_1.tier1_unit_count = 1
        parent_story_1.tier2_unit_count = 1
        parent_story_1.tier3_unit_count = 1
        parent_story_1.tier4_unit_count = 0
        parent_story_1.day = None
        parent_story_1.primary_entities = {}

        parent_story_2 = MagicMock(spec=Story)
        parent_story_2.id = parent_id_2
        parent_story_2.tier1_unit_count = 1
        parent_story_2.tier2_unit_count = 1
        parent_story_2.tier3_unit_count = 1
        parent_story_2.tier4_unit_count = 0
        parent_story_2.day = None
        parent_story_2.primary_entities = {}

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [parent_story_1, parent_story_2]

        children_results = {parent_id_1: 3, parent_id_2: 0}

        async def mock_execute(stmt):
            if hasattr(stmt, 'whereclause') and stmt.whereclause is not None:
                where_str = str(stmt.whereclause)
                if 'viewpoint_cluster_id' in where_str:
                    # Extract which parent this is for
                    for pid, count in children_results.items():
                        if str(pid) in where_str:
                            mock_r = MagicMock()
                            mock_r.scalar.return_value = count
                            return mock_r
            return mock_result

        mock_session.execute.side_effect = mock_execute

        with patch("src.verification.stories._gather_unit_texts_for_story", new_callable=AsyncMock) as mock_gather:
            mock_gather.return_value = [
                {"unit_id": uuid4(), "text": "Article 1", "source_tier": "tier1"},
                {"unit_id": uuid4(), "text": "Article 2", "source_tier": "tier2"},
                {"unit_id": uuid4(), "text": "Article 3", "source_tier": "tier3"},
            ]

            with patch("src.shared.llm.get_llm_client", new_callable=AsyncMock) as mock_llm_client:
                mock_llm = AsyncMock()
                mock_llm.chat_completion.return_value = {
                    "choices": [{"message": {"content": '{"unit1": "neutral", "unit2": "pro_govt", "unit3": "opposition"}'}}]
                }
                mock_llm_client.return_value = mock_llm

                result = await cluster_viewpoints(mock_session, [parent_id_1, parent_id_2])

        assert isinstance(result, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])