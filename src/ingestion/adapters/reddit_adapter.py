"""Reddit adapter wrapping ingest_reddit() from reddit.py."""
from src.ingestion.adapter import SourceHealth
from src.ingestion import reddit


class RedditAdapter:
    """Adapter for Reddit ingestion."""

    name = "reddit_tier3"

    def __init__(self):
        self._last_articles: list = []

    async def fetch(self) -> list:
        """Fetch articles from Reddit."""
        articles = await reddit.ingest_reddit(limit_per_sub=25)
        self._last_articles = articles
        return articles

    async def health_check(self) -> SourceHealth:
        """Return health from the last fetch."""
        if not self._last_articles:
            return SourceHealth(
                status="down",
                detail="No fetch performed yet",
                failed=["reddit.com"],
            )

        return SourceHealth(
            status="ok",
            detail=f"Fetched {len(self._last_articles)} articles",
            succeeded=["reddit.com"],
        )