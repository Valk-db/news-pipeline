"""Reddit adapter wrapping ingest_reddit() from reddit.py."""
from src.ingestion.adapter import SourceHealth
from src.ingestion import reddit
from src.shared.database import get_session
from src.schema.models import RawArticle
from sqlalchemy import select
from datetime import UTC


class RedditAdapter:
    """Adapter for Reddit ingestion."""

    name = "reddit_tier3"

    def __init__(self):
        self._last_articles: list = []

    async def fetch(self) -> list:
        """Fetch articles from Reddit."""
        # P1-1: Fetch known URL hashes from DB to dedup before extraction
        known_url_hashes = set()
        async with get_session() as session:
            from datetime import datetime, timedelta
            cutoff = datetime.now(UTC) - timedelta(days=30)
            stmt = select(RawArticle.url_hash).where(
                RawArticle.source_tier == "tier3",  # Reddit is tier-3
                RawArticle.fetched_at >= cutoff,
            )
            result = await session.execute(stmt)
            known_url_hashes = set(result.scalars().all())

        articles = await reddit.ingest_reddit(limit_per_sub=25, known_url_hashes=known_url_hashes)
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