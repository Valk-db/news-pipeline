"""End-to-end tests for RSS ingestion.

These tests verify the complete RSS ingestion flow without network calls,
by patching `rss.fetch_feed` and `rss.extract_article`.
"""

from unittest.mock import AsyncMock, patch

import pytest
from src.schema.models import SourceTier


class MockFeedEntry:
    """Mock feedparser entry that mimics dict-like and attribute access."""

    def __init__(self, link: str, title: str, summary: str = "Test summary"):
        self.link = link
        self.title = title
        self.summary = summary
        self.published_parsed = (2024, 1, 1, 12, 0, 0)
        self.updated_parsed = None
        self._attrs = {
            "link": link,
            "title": title,
            "summary": summary,
            "published_parsed": self.published_parsed,
            "updated_parsed": None,
        }

    def get(self, key, default=None):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs


class MockFeed:
    """Mock feedparser feed."""

    def __init__(self, entries):
        self.entries = entries


def make_sufficient_body_text() -> str:
    """Body text long enough to pass the 200-char minimum in process_feed_entry."""
    return (
        "Body text long enough to pass the minimum requirement for article "
        "extraction and processing. This sentence exists purely to push the "
        "mock content past the two-hundred-character floor enforced in "
        "process_feed_entry so the RSS ingestion test can exercise the real "
        "content-hash path instead of short-circuiting on it."
    )


async def run_ingest_with_mocks(sources: dict, feeds: dict):
    """Run ingest_rss_feeds with mocked fetch_feed and extract_article.

    Args:
        sources: Source configuration dict
        feeds: Mapping of feed_url -> MockFeed

    Returns:
        List of RawArticle objects
    """
    from src.ingestion.rss import ingest_rss_feeds

    call_count = 0

    async def mock_fetch(_client, feed_url, timeout=30, source_key=""):
        nonlocal call_count
        call_count += 1
        return feeds.get(feed_url, MockFeed([]))

    with patch("src.ingestion.rss.extract_article", new_callable=AsyncMock) as mock_extract, \
         patch("src.ingestion.rss.extract_entities_top_n", return_value={"PERSON": [], "ORG": ["BBC"], "GPE": []}), \
         patch("src.ingestion.rss.compute_url_hash", side_effect=lambda x: f"hash_{x}"), \
         patch("src.ingestion.rss.compute_content_hash", return_value="content_hash"), \
         patch("src.ingestion.rss.fetch_feed", new_callable=AsyncMock) as mock_fetch_fn:

        mock_extract.return_value = (make_sufficient_body_text(), "Extracted Title")
        mock_fetch_fn.side_effect = mock_fetch

        articles = await ingest_rss_feeds(sources=sources, max_per_feed=50)
        return articles


class TestRSSIngestE2E:
    """End-to-end tests for RSS ingestion flow."""

    @pytest.mark.asyncio
    async def test_n_entries_produce_n_articles(self):
        """N feed entries should produce N RawArticles."""
        sources = {
            "bbc": {
                "name": "BBC News",
                "domain": "bbc.com",
                "tier": SourceTier.TIER1,
                "feeds": ["https://feeds.bbci.co.uk/news/world/rss.xml"],
            }
        }

        feeds = {
            "https://feeds.bbci.co.uk/news/world/rss.xml": MockFeed([
                MockFeedEntry("https://bbc.com/article/1", "Article 1"),
                MockFeedEntry("https://bbc.com/article/2", "Article 2"),
                MockFeedEntry("https://bbc.com/article/3", "Article 3"),
                MockFeedEntry("https://bbc.com/article/4", "Article 4"),
                MockFeedEntry("https://bbc.com/article/5", "Article 5"),
            ]),
        }

        articles = await run_ingest_with_mocks(sources, feeds)

        assert len(articles) == 5, f"Expected 5 articles, got {len(articles)}"
        for i, article in enumerate(articles):
            assert article.url == f"https://bbc.com/article/{i+1}"
            assert article.source_domain == "bbc.com"
            assert article.source_tier == SourceTier.TIER1
            assert article.content_hash == "content_hash"

    @pytest.mark.asyncio
    async def test_same_url_in_two_feeds_one_source_deduped(self):
        """Same URL appearing in two feeds of one source -> exactly 1 article."""
        sources = {
            "bbc": {
                "name": "BBC News",
                "domain": "bbc.com",
                "tier": SourceTier.TIER1,
                "feeds": [
                    "https://feeds.bbci.co.uk/news/world/rss.xml",
                    "https://feeds.bbci.co.uk/news/uk/rss.xml",
                ],
            }
        }

        feeds = {
            "https://feeds.bbci.co.uk/news/world/rss.xml": MockFeed([
                MockFeedEntry("https://bbc.com/article/1", "Article 1"),
                MockFeedEntry("https://bbc.com/article/2", "Article 2"),
            ]),
            "https://feeds.bbci.co.uk/news/uk/rss.xml": MockFeed([
                MockFeedEntry("https://bbc.com/article/1", "Article 1 Duplicate"),  # Same URL
                MockFeedEntry("https://bbc.com/article/3", "Article 3"),
            ]),
        }

        articles = await run_ingest_with_mocks(sources, feeds)

        assert len(articles) == 3, f"Expected 3 unique articles, got {len(articles)}"
        urls = {a.url for a in articles}
        assert urls == {
            "https://bbc.com/article/1",
            "https://bbc.com/article/2",
            "https://bbc.com/article/3",
        }

    @pytest.mark.asyncio
    async def test_same_url_across_two_sources_deduped(self):
        """Same URL across two sources -> exactly 1 article."""
        sources = {
            "ap": {
                "name": "AP News",
                "domain": "apnews.com",
                "tier": SourceTier.TIER1,
                "feeds": ["https://apnews.com/rss1"],
            },
            "reuters": {
                "name": "Reuters",
                "domain": "reuters.com",
                "tier": SourceTier.TIER1,
                "feeds": ["https://reuters.com/rss1"],
            },
        }

        feeds = {
            "https://apnews.com/rss1": MockFeed([
                MockFeedEntry("https://apnews.com/article/1", "AP Article 1"),
            ]),
            "https://reuters.com/rss1": MockFeed([
                MockFeedEntry("https://apnews.com/article/1", "AP Article 1 Duplicate"),  # Same URL, different source
            ]),
        }

        articles = await run_ingest_with_mocks(sources, feeds)

        assert len(articles) == 1, f"Expected 1 unique article (cross-source dedup), got {len(articles)}"
        assert articles[0].url == "https://apnews.com/article/1"
        # First source (AP) wins because its feed is processed first
        assert articles[0].source_domain == "apnews.com"

    @pytest.mark.asyncio
    async def test_entry_with_no_link_skipped_others_returned(self):
        """Entry with no link should be skipped; others still returned."""
        sources = {
            "bbc": {
                "name": "BBC News",
                "domain": "bbc.com",
                "tier": SourceTier.TIER1,
                "feeds": ["https://feeds.bbci.co.uk/news/world/rss.xml"],
            }
        }

        feeds = {
            "https://feeds.bbci.co.uk/news/world/rss.xml": MockFeed([
                MockFeedEntry("https://bbc.com/article/1", "Article 1"),
                MockFeedEntry("", "Article with no link"),  # No link
                MockFeedEntry("https://bbc.com/article/2", "Article 2"),
            ]),
        }

        articles = await run_ingest_with_mocks(sources, feeds)

        assert len(articles) == 2, f"Expected 2 articles (one skipped), got {len(articles)}"
        urls = {a.url for a in articles}
        assert urls == {
            "https://bbc.com/article/1",
            "https://bbc.com/article/2",
        }

    @pytest.mark.asyncio
    async def test_entry_with_no_title_skipped_others_returned(self):
        """Entry with no title should be skipped; others still returned."""
        sources = {
            "bbc": {
                "name": "BBC News",
                "domain": "bbc.com",
                "tier": SourceTier.TIER1,
                "feeds": ["https://feeds.bbci.co.uk/news/world/rss.xml"],
            }
        }

        # Create an entry with empty title
        class NoTitleEntry(MockFeedEntry):
            def __init__(self, link):
                super().__init__(link, "")  # Empty title

        feeds = {
            "https://feeds.bbci.co.uk/news/world/rss.xml": MockFeed([
                MockFeedEntry("https://bbc.com/article/1", "Article 1"),
                NoTitleEntry("https://bbc.com/article/2"),  # No title
                MockFeedEntry("https://bbc.com/article/3", "Article 3"),
            ]),
        }

        articles = await run_ingest_with_mocks(sources, feeds)

        assert len(articles) == 2, f"Expected 2 articles (one skipped), got {len(articles)}"
        urls = {a.url for a in articles}
        assert urls == {
            "https://bbc.com/article/1",
            "https://bbc.com/article/3",
        }

    @pytest.mark.asyncio
    async def test_both_no_link_and_no_title_entries_skipped(self):
        """Multiple bad entries should all be skipped; good ones returned."""
        sources = {
            "bbc": {
                "name": "BBC News",
                "domain": "bbc.com",
                "tier": SourceTier.TIER1,
                "feeds": ["https://feeds.bbci.co.uk/news/world/rss.xml"],
            }
        }

        class NoTitleEntry(MockFeedEntry):
            def __init__(self, link):
                super().__init__(link, "")

        feeds = {
            "https://feeds.bbci.co.uk/news/world/rss.xml": MockFeed([
                MockFeedEntry("", "No link"),
                NoTitleEntry("https://bbc.com/article/2"),
                MockFeedEntry("https://bbc.com/article/1", "Article 1"),
                MockFeedEntry("", "Another no link"),
            ]),
        }

        articles = await run_ingest_with_mocks(sources, feeds)

        assert len(articles) == 1, f"Expected 1 article (three skipped), got {len(articles)}"
        assert articles[0].url == "https://bbc.com/article/1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])