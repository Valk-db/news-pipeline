"""Tests for GDELT toggle via settings.gdelt_enabled."""

from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from src.ingestion.run import run_ingestion


class TestGdeltToggle:
    """Test that gdelt_enabled controls GDELT ingestion."""

    @pytest.mark.asyncio
    async def test_gdelt_disabled_never_called(self):
        """With gdelt_enabled=False, ingest_gdelt is never called."""
        with patch("src.ingestion.gdelt.ingest_gdelt", new_callable=AsyncMock) as mock_gdelt:
            with patch("src.ingestion.rss.ingest_rss_feeds", new_callable=AsyncMock) as mock_rss:
                with patch("src.ingestion.reddit.ingest_reddit", new_callable=AsyncMock) as mock_reddit:
                    with patch("src.ingestion.run.build_reporting_units", new_callable=AsyncMock):
                        with patch("src.ingestion.run.build_stories", new_callable=AsyncMock):
                            with patch("src.ingestion.run.apply_dynamic_gate", new_callable=AsyncMock):
                                with patch("src.ingestion.run.get_settings") as mock_settings:
                                    settings = MagicMock()
                                    settings.gdelt_enabled = False
                                    settings.max_articles_per_feed = 50
                                    settings.rss_fetch_timeout = 30
                                    mock_settings.return_value = settings

                                    mock_rss.return_value = []
                                    mock_reddit.return_value = []

                                    mock_session = AsyncMock()
                                    with patch("src.ingestion.run.get_session") as mock_get_session:
                                        mock_get_session.return_value.__aenter__.return_value = mock_session

                                        results = await run_ingestion(dry_run=True)

                                    # GDELT should not be called
                                    mock_gdelt.assert_not_called()
                                    # Results should show health from fallback (GDELT adapter not run)
                                    gdelt_health = results["phases"]["ingestion"]["gdelt_health"]
                                    assert gdelt_health["succeeded"] == []
                                    assert gdelt_health["failed"] == []
                                    assert gdelt_health["skipped"] == []

    @pytest.mark.asyncio
    async def test_gdelt_enabled_called_once(self):
        """With gdelt_enabled=True, ingest_gdelt is called once."""
        with patch("src.ingestion.gdelt.ingest_gdelt", new_callable=AsyncMock) as mock_gdelt:
            with patch("src.ingestion.rss.ingest_rss_feeds", new_callable=AsyncMock) as mock_rss:
                with patch("src.ingestion.reddit.ingest_reddit", new_callable=AsyncMock) as mock_reddit:
                    with patch("src.ingestion.run.build_reporting_units", new_callable=AsyncMock):
                        with patch("src.ingestion.run.build_stories", new_callable=AsyncMock):
                            with patch("src.ingestion.run.apply_dynamic_gate", new_callable=AsyncMock):
                                with patch("src.ingestion.run.get_settings") as mock_settings:
                                    settings = MagicMock()
                                    settings.gdelt_enabled = True
                                    settings.max_articles_per_feed = 50
                                    settings.rss_fetch_timeout = 30
                                    mock_settings.return_value = settings

                                    mock_rss.return_value = []
                                    mock_reddit.return_value = []
                                    mock_gdelt.return_value = ([], {"succeeded": [], "failed": [], "skipped": []})

                                    mock_session = AsyncMock()
                                    with patch("src.ingestion.run.get_session") as mock_get_session:
                                        mock_get_session.return_value.__aenter__.return_value = mock_session

                                        results = await run_ingestion(dry_run=True)

                                    # GDELT should be called exactly once
                                    mock_gdelt.assert_called_once()
                                    # Results should show health from GDELT adapter
                                    gdelt_health = results["phases"]["ingestion"]["gdelt_health"]
                                    assert gdelt_health["succeeded"] == []
                                    assert gdelt_health["failed"] == []
                                    assert gdelt_health["skipped"] == []