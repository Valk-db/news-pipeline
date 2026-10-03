"""RSS adapter wrapping ingest_rss_feeds() from rss.py.

One adapter instance per tier (RssAdapter(tier=SourceTier.TIER1),
RssAdapter(tier=SourceTier.TIER2)), sourcing its feed list from
get_enabled_sources_by_tier() in source_registry.py exactly as run.py
does today.
"""
from src.ingestion.adapter import SourceHealth
from src.ingestion import rss
from src.ingestion.source_registry import get_enabled_sources_by_tier, SourceTier
from src.shared.database import get_session
from src.schema.models import RawArticle
from sqlalchemy import select


class RssAdapter:
    """Adapter for RSS ingestion sources."""

    def __init__(self, tier: SourceTier, domains: set[str] | None = None):
        self.tier = tier
        # Registry domains this adapter is allowed to sweep. None means every
        # enabled source in the tier; a set narrows the sweep to those domains,
        # which is what `--sources <domain>` needs. Without the narrowing a
        # domain-filtered run still polled the whole tier.
        self.domains = domains
        self.name = f"rss_{tier.name.lower()}"
        self._last_fetch_articles: list = []
        self._last_fetch_stats: dict = {}
        self._fetch_called = False

    def sources(self) -> dict:
        """The registry sources this adapter is allowed to sweep."""
        enabled = get_enabled_sources_by_tier(self.tier)
        if self.domains is None:
            return enabled
        return {d: cfg for d, cfg in enabled.items() if d in self.domains}

    async def fetch(self) -> list:
        """Fetch articles from RSS feeds for the configured tier."""
        sources = self.sources()

        # P1-1: Fetch known URL hashes from DB to dedup before extraction
        known_url_hashes = set()
        async with get_session() as session:
            # Get URL hashes from last 30 days (covers recent runs)
            from datetime import datetime, timezone, timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            stmt = select(RawArticle.url_hash).where(
                RawArticle.source_tier == self.tier,
                RawArticle.fetched_at >= cutoff,
            )
            result = await session.execute(stmt)
            known_url_hashes = set(result.scalars().all())

        articles = await rss.ingest_rss_feeds(max_per_feed=50, sources=sources, known_url_hashes=known_url_hashes)
        self._last_fetch_articles = articles
        # Track per-domain success/failure from stats
        from src.utils.ingest_stats import STATS
        self._last_fetch_stats = STATS.snapshot()
        self._fetch_called = True
        return articles

    async def health_check(self) -> SourceHealth:
        """Derive health from the last fetch() results.

        For tier-1: degraded if any enabled tier-1 domain produced zero
        ok articles (this is the same check main() already does per-source
        after the run today — moved into the adapter).
        """
        if not self._fetch_called:
            return SourceHealth(
                status="down",
                detail="No fetch performed yet",
                failed=[s.domain for s in self.sources().values()],
            )

        # Check per-domain success for tier-1
        if self.tier == SourceTier.TIER1:
            sources = self.sources()
            failed_domains = []
            for domain in sources.keys():
                ok_count = self._last_fetch_stats.get(f"{domain}.ok", 0)
                if ok_count == 0:
                    failed_domains.append(domain)

            if failed_domains:
                return SourceHealth(
                    status="degraded",
                    detail=f"Tier-1 domains with zero articles: {', '.join(failed_domains)}",
                    failed=failed_domains,
                    succeeded=[d for d in sources.keys() if d not in failed_domains],
                )

        # For tier-2 or if all tier-1 domains succeeded
        succeeded_domains = [a.source_domain for a in self._last_fetch_articles]
        return SourceHealth(
            status="ok",
            detail=f"Fetched {len(self._last_fetch_articles)} articles",
            succeeded=succeeded_domains,
        )