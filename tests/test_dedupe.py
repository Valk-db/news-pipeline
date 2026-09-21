"""Tests for the dedupe_articles pure function."""

from src.ingestion.run import dedupe_articles
from src.schema.models import RawArticle, SourceTier
import uuid


def make_article(url: str, content_hash: str = None, source_domain: str = "bbc.com") -> RawArticle:
    """Create a minimal RawArticle for testing."""
    return RawArticle(
        id=uuid.uuid4(),
        url=url,
        url_hash=f"url_hash_{url}",
        title=f"Title for {url}",
        body_text="x" * 200,
        source_domain=source_domain,
        source_tier=SourceTier.TIER1,
        content_hash=content_hash or f"content_hash_{url}",
    )


class TestDedupeArticles:
    """Test the dedupe_articles function."""

    def test_same_url_from_two_sources_keeps_first(self):
        """Same URL from two sources keeps 1 (first wins)."""
        existing_url = set()
        existing_content = {}
        articles = [
            make_article("https://example.com/article", source_domain="bbc.com"),
            make_article("https://example.com/article", source_domain="theguardian.com"),
        ]
        new, url_dup, content_dup = dedupe_articles(articles, existing_url, existing_content)
        assert len(new) == 1
        assert url_dup == 1
        assert content_dup == 0
        assert new[0].source_domain == "bbc.com"  # First wins

    def test_same_content_hash_same_domain_keeps_first(self):
        """Same (content_hash, domain) twice keeps 1."""
        existing_url = set()
        existing_content = {}
        articles = [
            make_article("https://example.com/a", content_hash="same_hash", source_domain="bbc.com"),
            make_article("https://example.com/b", content_hash="same_hash", source_domain="bbc.com"),
        ]
        new, url_dup, content_dup = dedupe_articles(articles, existing_url, existing_content)
        assert len(new) == 1
        assert url_dup == 0
        assert content_dup == 1

    def test_same_content_hash_different_domains_keeps_both(self):
        """Same content on different domains keeps both."""
        existing_url = set()
        existing_content = {}
        articles = [
            make_article("https://example.com/a", content_hash="same_hash", source_domain="bbc.com"),
            make_article("https://example.com/b", content_hash="same_hash", source_domain="theguardian.com"),
        ]
        new, url_dup, content_dup = dedupe_articles(articles, existing_url, existing_content)
        assert len(new) == 2
        assert url_dup == 0
        assert content_dup == 0

    def test_already_in_db_skipped(self):
        """Already in DB (by URL hash) still skipped."""
        existing_url = {"url_hash_https://example.com/a"}
        existing_content = {}
        articles = [
            make_article("https://example.com/a", source_domain="bbc.com"),
        ]
        new, url_dup, content_dup = dedupe_articles(articles, existing_url, existing_content)
        assert len(new) == 0
        assert url_dup == 1

    def test_already_in_db_content_hash_skipped(self):
        """Already in DB (by content hash + same domain) still skipped."""
        existing_url = set()
        existing_content = {"same_hash": {"bbc.com"}}
        articles = [
            make_article("https://example.com/new", content_hash="same_hash", source_domain="bbc.com"),
        ]
        new, url_dup, content_dup = dedupe_articles(articles, existing_url, existing_content)
        assert len(new) == 0
        assert content_dup == 1

    def test_content_hash_different_domain_not_skipped(self):
        """Content hash exists in DB but different domain -> not skipped."""
        existing_url = set()
        existing_content = {"same_hash": {"bbc.com"}}
        articles = [
            make_article("https://example.com/new", content_hash="same_hash", source_domain="theguardian.com"),
        ]
        new, url_dup, content_dup = dedupe_articles(articles, existing_url, existing_content)
        assert len(new) == 1
        assert content_dup == 0

    def test_batch_and_db_duplicates(self):
        """Duplicates both within batch and against DB."""
        existing_url = {"url_hash_https://example.com/db_dup"}
        existing_content = {"db_content_hash": {"bbc.com"}}
        # Use explicit url_hash for batch duplicates
        articles = [
            make_article("https://example.com/db_dup", source_domain="bbc.com"),  # URL dup vs DB
            RawArticle(
                id=uuid.uuid4(),
                url="https://example.com/batch_dup1",
                url_hash="batch_url_hash",  # Same for both
                title="Batch dup 1",
                body_text="x" * 200,
                source_domain="bbc.com",
                source_tier=SourceTier.TIER1,
                content_hash="batch_dup",
            ),
            RawArticle(
                id=uuid.uuid4(),
                url="https://example.com/batch_dup2",
                url_hash="batch_url_hash",  # Same for both
                title="Batch dup 2",
                body_text="x" * 200,
                source_domain="bbc.com",
                source_tier=SourceTier.TIER1,
                content_hash="batch_dup",
            ),
            make_article("https://example.com/db_content_dup", content_hash="db_content_hash", source_domain="bbc.com"),  # Content dup vs DB
            make_article("https://example.com/ok", source_domain="bbc.com"),  # OK
        ]
        new, url_dup, content_dup = dedupe_articles(articles, existing_url, existing_content)
        # First batch_dup article is kept (first wins), second is skipped (URL dup)
        # db_dup is URL-dup vs DB, db_content_dup is content-dup vs DB
        # Note: second batch_dup is caught as URL dup (checked first), not content dup
        assert len(new) == 2  # batch_dup1 (first) + ok
        assert url_dup == 2  # 1 URL dup vs DB (db_dup) + 1 batch URL dup (second batch_dup)
        assert content_dup == 1  # 1 DB content dup (db_content_dup)

    def test_empty_inputs(self):
        """Empty inputs return empty results."""
        new, url_dup, content_dup = dedupe_articles([], set(), {})
        assert len(new) == 0
        assert url_dup == 0
        assert content_dup == 0

    def test_order_preserved_first_wins(self):
        """Order is preserved - first article wins."""
        existing_url = set()
        existing_content = {}
        articles = [
            make_article("https://example.com/a", source_domain="bbc.com"),
            make_article("https://example.com/b", source_domain="theguardian.com"),
            make_article("https://example.com/a", source_domain="npr.org"),  # URL dup
        ]
        new, url_dup, content_dup = dedupe_articles(articles, existing_url, existing_content)
        assert len(new) == 2
        assert [a.url for a in new] == ["https://example.com/a", "https://example.com/b"]