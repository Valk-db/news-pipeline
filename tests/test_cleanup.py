"""Tests for cleanup job."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta
import uuid

from src.verification.cleanup import (
    CleanupResult,
    cleanup_stale_stories,
    cleanup_orphaned_reporting_units,
    cleanup_stale_story_links,
    run_cleanup,
)
from src.schema.models import Story, ReportingUnit, StoryUnitLink


class TestCleanupResult:
    """Tests for CleanupResult dataclass."""

    def test_defaults(self):
        result = CleanupResult()
        assert result.stories_expired == 0
        assert result.orphaned_units_removed == 0
        assert result.stale_links_removed == 0
        assert result.details == []

    def test_custom_values(self):
        result = CleanupResult(
            stories_expired=5,
            orphaned_units_removed=3,
            stale_links_removed=1,
            details=["detail1", "detail2"]
        )
        assert result.stories_expired == 5
        assert result.orphaned_units_removed == 3
        assert result.stale_links_removed == 1
        assert result.details == ["detail1", "detail2"]


class TestCleanupStaleStories:
    """Tests for cleanup_stale_stories function."""

    @pytest.mark.asyncio
    async def test_expires_pending_stories(self):
        """PENDING stories older than threshold become EXPIRED."""
        mock_session = AsyncMock()
        now = datetime.now(timezone.utc)

        # Create mock stories
        old_pending = MagicMock(spec=Story)
        old_pending.id = uuid.uuid4()
        old_pending.status = Story.Status.PENDING
        old_pending.updated_at = now - timedelta(hours=100)  # Older than 72h
        old_pending.gate_reason = None

        recent_pending = MagicMock(spec=Story)
        recent_pending.id = uuid.uuid4()
        recent_pending.status = Story.Status.PENDING
        recent_pending.updated_at = now - timedelta(hours=10)  # Within threshold

        blocked_story = MagicMock(spec=Story)
        blocked_story.id = uuid.uuid4()
        blocked_story.status = Story.Status.BLOCKED
        blocked_story.updated_at = now - timedelta(hours=200)  # Older than 168h

        # Mock execute to return different stories for different queries
        # The function makes 2 calls: first for PENDING, second for BLOCKED
        # Since SQL is not actually executed, the mock must do the filtering
        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 1:  # First call - PENDING query
                # Filter: only stories where updated_at < now - 72h
                pending_cutoff = now - timedelta(hours=72)
                matching = [s for s in [old_pending, recent_pending] if s.updated_at < pending_cutoff]
                result.scalars.return_value.all.return_value = matching
            elif call_count[0] == 2:  # Second call - BLOCKED query
                blocked_cutoff = now - timedelta(hours=168)
                matching = [s for s in [blocked_story] if s.updated_at < blocked_cutoff]
                result.scalars.return_value.all.return_value = matching
            else:
                result.scalars.return_value.all.return_value = []
            return result

        mock_session.execute.side_effect = mock_execute
        mock_session.flush = AsyncMock()

        result = await cleanup_stale_stories(mock_session, pending_hours=72, blocked_hours=168)

        assert result.stories_expired == 2  # old_pending + blocked_story
        assert old_pending.status == Story.Status.EXPIRED
        assert blocked_story.status == Story.Status.EXPIRED
        assert recent_pending.status == Story.Status.PENDING  # Not expired

    @pytest.mark.asyncio
    async def test_respects_different_thresholds(self):
        """PENDING and BLOCKED have different thresholds."""
        mock_session = AsyncMock()
        now = datetime.now(timezone.utc)

        pending_at_50h = MagicMock(spec=Story)
        pending_at_50h.id = uuid.uuid4()
        pending_at_50h.status = Story.Status.PENDING
        pending_at_50h.updated_at = now - timedelta(hours=50)  # < 72h

        blocked_at_100h = MagicMock(spec=Story)
        blocked_at_100h.id = uuid.uuid4()
        blocked_at_100h.status = Story.Status.BLOCKED
        blocked_at_100h.updated_at = now - timedelta(hours=100)  # > 72h but < 168h

        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 1:
                # PENDING query: filter by 72h threshold
                pending_cutoff = now - timedelta(hours=72)
                matching = [s for s in [pending_at_50h] if s.updated_at < pending_cutoff]
                result.scalars.return_value.all.return_value = matching
            else:
                # BLOCKED query: filter by 168h threshold
                blocked_cutoff = now - timedelta(hours=168)
                matching = [s for s in [blocked_at_100h] if s.updated_at < blocked_cutoff]
                result.scalars.return_value.all.return_value = matching
            return result

        mock_session.execute.side_effect = mock_execute
        mock_session.flush = AsyncMock()

        result = await cleanup_stale_stories(mock_session, pending_hours=72, blocked_hours=168)

        assert result.stories_expired == 0  # Neither crosses its threshold
        assert pending_at_50h.status == Story.Status.PENDING
        assert blocked_at_100h.status == Story.Status.BLOCKED


class TestCleanupOrphanedReportingUnits:
    """Tests for cleanup_orphaned_reporting_units function."""

    @pytest.mark.asyncio
    async def test_removes_old_unlinked_units(self):
        """Old unlinked units with no articles are removed."""
        mock_session = AsyncMock()
        now = datetime.now(timezone.utc)

        old_unlinked = MagicMock(spec=ReportingUnit)
        old_unlinked.id = uuid.uuid4()
        old_unlinked.created_at = now - timedelta(hours=48)  # Old enough
        old_unlinked.article_count = 0

        recent_unlinked = MagicMock(spec=ReportingUnit)
        recent_unlinked.id = uuid.uuid4()
        recent_unlinked.created_at = now - timedelta(hours=12)  # Too recent

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [old_unlinked, recent_unlinked]
        mock_session.execute.return_value = mock_result
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        result = await cleanup_orphaned_reporting_units(mock_session)

        assert result.orphaned_units_removed == 1
        mock_session.delete.assert_called_once_with(old_unlinked)

    @pytest.mark.asyncio
    async def test_skips_linked_units(self):
        """Units with story links are not considered orphaned."""
        mock_session = AsyncMock()

        # The query uses outerjoin with WHERE link.id IS NULL
        # So linked units won't even be returned
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result
        mock_session.flush = AsyncMock()

        result = await cleanup_orphaned_reporting_units(mock_session)

        assert result.orphaned_units_removed == 0


class TestCleanupStaleStoryLinks:
    """Tests for cleanup_stale_story_links function."""

    @pytest.mark.asyncio
    async def test_removes_links_to_deleted_stories(self):
        """Links pointing to non-existent stories/units are removed."""
        mock_session = AsyncMock()

        stale_link = MagicMock(spec=StoryUnitLink)
        stale_link.id = uuid.uuid4()
        stale_link.story_id = uuid.uuid4()
        stale_link.unit_id = uuid.uuid4()

        valid_link = MagicMock(spec=StoryUnitLink)
        valid_link.id = uuid.uuid4()
        valid_link.story_id = uuid.uuid4()
        valid_link.unit_id = uuid.uuid4()

        # New implementation uses single query with LEFT JOINs
        # Return stale_link (missing story/unit) but NOT valid_link (both exist)
        mock_link_result = MagicMock()
        mock_link_result.scalars.return_value.all.return_value = [stale_link]

        async def mock_execute(stmt):
            return mock_link_result

        mock_session.execute.side_effect = mock_execute
        mock_session.delete = AsyncMock()
        mock_session.flush = AsyncMock()

        result = await cleanup_stale_story_links(mock_session)

        assert result.stale_links_removed == 1
        mock_session.delete.assert_called_once_with(stale_link)


class TestRunCleanup:
    """Tests for run_cleanup integration."""

    @pytest.mark.asyncio
    async def test_runs_all_subtasks(self):
        """run_cleanup calls all three cleanup functions."""
        mock_session = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()  # For StatusLog insert

        with patch("src.verification.cleanup.cleanup_stale_stories") as mock_stories, \
             patch("src.verification.cleanup.cleanup_orphaned_reporting_units") as mock_units, \
             patch("src.verification.cleanup.cleanup_stale_story_links") as mock_links:

            mock_stories.return_value = CleanupResult(stories_expired=2, details=["expired 1", "expired 2"])
            mock_units.return_value = CleanupResult(orphaned_units_removed=1, details=["removed unit 1"])
            mock_links.return_value = CleanupResult(stale_links_removed=3, details=["link 1", "link 2", "link 3"])

            result = await run_cleanup(mock_session)

            assert result.stories_expired == 2
            assert result.orphaned_units_removed == 1
            assert result.stale_links_removed == 3
            assert len(result.details) == 6

            mock_stories.assert_called_once()
            mock_units.assert_called_once()
            mock_links.assert_called_once()
            mock_session.commit.assert_called_once()


class TestIntegration:
    """Integration-style tests."""

    @pytest.mark.asyncio
    async def test_queued_expires_posted_rejected_never(self):
        """QUEUED stories expire after threshold; POSTED/REJECTED never expire."""
        mock_session = AsyncMock()
        now = datetime.now(timezone.utc)

        # QUEUED story older than 168h - SHOULD be expired
        queued_old = MagicMock(spec=Story)
        queued_old.id = uuid.uuid4()
        queued_old.status = Story.Status.QUEUED
        queued_old.updated_at = now - timedelta(hours=200)

        # QUEUED story within threshold - should NOT be expired
        queued_recent = MagicMock(spec=Story)
        queued_recent.id = uuid.uuid4()
        queued_recent.status = Story.Status.QUEUED
        queued_recent.updated_at = now - timedelta(hours=50)

        # POSTED story - should NEVER be expired
        posted_old = MagicMock(spec=Story)
        posted_old.id = uuid.uuid4()
        posted_old.status = Story.Status.POSTED
        posted_old.updated_at = now - timedelta(hours=200)

        # REJECTED story - should NEVER be expired
        rejected_old = MagicMock(spec=Story)
        rejected_old.id = uuid.uuid4()
        rejected_old.status = Story.Status.REJECTED
        rejected_old.updated_at = now - timedelta(hours=200)

        call_count = [0]
        async def mock_execute(stmt):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 1:  # PENDING query
                result.scalars.return_value.all.return_value = []
            elif call_count[0] == 2:  # BLOCKED query
                result.scalars.return_value.all.return_value = []
            elif call_count[0] == 3:  # QUEUED query - filter by queued_hours (168h)
                queued_cutoff = now - timedelta(hours=168)
                matching = [s for s in [queued_old, queued_recent] if s.updated_at < queued_cutoff]
                result.scalars.return_value.all.return_value = matching
            else:
                result.scalars.return_value.all.return_value = []
            return result

        mock_session.execute.side_effect = mock_execute
        mock_session.flush = AsyncMock()

        result = await cleanup_stale_stories(mock_session, pending_hours=72, blocked_hours=168, queued_hours=168)

        # Only queued_old should be expired (1 story)
        assert result.stories_expired == 1
        assert queued_old.status == Story.Status.EXPIRED
        assert queued_recent.status == Story.Status.QUEUED  # Not expired (within threshold)
        assert posted_old.status == Story.Status.POSTED  # Never expires
        assert rejected_old.status == Story.Status.REJECTED  # Never expires


if __name__ == "__main__":
    pytest.main([__file__, "-v"])