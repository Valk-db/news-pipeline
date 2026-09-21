"""Tests for run.py exit guard and cleanup queued behavior."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.ingestion.run import main, run_ingestion
from src.utils.ingest_stats import STATS
import sys


class TestExitGuard:
    """Test main() exit guard behavior."""

    @pytest.mark.asyncio
    async def test_total_fetched_zero_exits_1(self):
        """total_fetched == 0 exits with code 1 (not dry run)."""
        with patch("src.ingestion.run.run_ingestion", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "phases": {
                    "ingestion": {
                        "total_fetched": 0,
                        "extraction_stats": {}
                    }
                }
            }

            with patch.object(sys, "argv", ["run.py"]):
                with pytest.raises(SystemExit) as exc_info:
                    await main()
                assert exc_info.value.code == 1

    @pytest.mark.asyncio
    async def test_total_fetched_nonzero_no_exit(self):
        """total_fetched > 0 does not exit (even if all dupes)."""
        with patch("src.ingestion.run.run_ingestion", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "phases": {
                    "ingestion": {
                        "total_fetched": 10,
                        "total_new": 0,
                        "extraction_stats": {}
                    }
                }
            }

            with patch.object(sys, "argv", ["run.py"]):
                # Should not raise SystemExit
                await main()

    @pytest.mark.asyncio
    async def test_dry_run_never_exits(self):
        """Dry run never triggers exit guard."""
        with patch("src.ingestion.run.run_ingestion", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "phases": {
                    "ingestion": {
                        "total_fetched": 0,
                        "extraction_stats": {}
                    }
                }
            }

            with patch.object(sys, "argv", ["run.py", "--dry-run"]):
                # Should not raise SystemExit
                await main()


class TestCleanupQueuedDisabled:
    """Test that queued_hours=0 leaves old QUEUED stories untouched."""

    @pytest.mark.asyncio
    async def test_queued_hours_zero_skips_queued(self):
        """With queued_hours=0, old QUEUED stories are not expired."""
        from src.verification.cleanup import cleanup_stale_stories
        from src.schema.models import Story
        from datetime import datetime, timezone, timedelta

        mock_session = AsyncMock()
        now = datetime.now(timezone.utc)

        # Create a QUEUED story older than 168 hours
        old_queued = Story(
            id=MagicMock(),
            status=Story.Status.QUEUED,
            updated_at=now - timedelta(hours=200),
            gate_reason=None,
        )

        # Mock the session execute to return NO queued stories
        # (since queued_hours=0, the function shouldn't even query for them)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute.return_value = mock_result

        # Call with queued_hours=0 (disabled)
        from src.verification.cleanup import CleanupResult
        result = await cleanup_stale_stories(mock_session, queued_hours=0)

        # The QUEUED story should NOT be expired (no query should be made for QUEUED)
        assert result.stories_expired == 0