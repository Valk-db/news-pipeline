"""Sensor adapter wrapping the hazard feeds in sensors.py.

Feeds USGS earthquakes and GDACS alerts into the same raw article path as RSS
and GDELT: fetch() returns RawArticle objects built from the article shaped
dicts sensors.py produces, and health_check() reports on the last fetch rather
than making a fresh network call.
"""

import logging

from src.ingestion import sensors
from src.ingestion.adapter import SourceHealth
from src.schema.models import RawArticle, SourceTier

logger = logging.getLogger(__name__)


def _to_raw_article(record: dict) -> RawArticle | None:
    """Convert a sensors.py article dict into a RawArticle row."""
    url = record.get("url")
    title = record.get("title")
    if not url or not title:
        return None
    tier = record.get("source_tier") or SourceTier.TIER2
    return RawArticle(
        url=url,
        url_hash=record["url_hash"],
        title=title,
        body_text=record.get("body_text"),
        summary=record.get("summary"),
        source_domain=record.get("source_domain", "unknown"),
        source_tier=tier if isinstance(tier, SourceTier) else SourceTier(tier),
        published_at=record.get("published_at"),
        entities=record.get("entities"),
        content_hash=record.get("content_hash"),
    )


class SensorAdapter:
    """Adapter for the USGS and GDACS hazard sensors."""

    name = "sensors"

    def __init__(self, feeds: list[str] | None = None):
        # feed keys are "usgs_earthquakes" and "gdacs_alerts"
        self.feeds = feeds or ["usgs_earthquakes", "gdacs_alerts"]
        self._last_articles: list = []
        self._last_health: dict = {}
        self._fetch_called = False

    async def fetch(self) -> list:
        """Fetch every configured sensor feed. Returns RawArticle objects."""
        articles: list = []
        health: dict = {}

        for feed_key in self.feeds:
            producer = getattr(sensors, feed_key, None)
            if producer is None:
                logger.warning("Sensors: unknown feed %s", feed_key)
                health[feed_key] = "unknown_feed"
                continue
            try:
                records = producer()
            except Exception as e:
                # One dead sensor must not sink the run.
                logger.warning("Sensors: feed %s failed: %s", feed_key, e)
                health[feed_key] = "error"
                continue

            produced = 0
            for record in records:
                article = _to_raw_article(record)
                if article is None:
                    continue
                articles.append(article)
                produced += 1
            health[feed_key] = produced

        self._last_articles = articles
        self._last_health = health
        self._fetch_called = True
        return articles

    async def health_check(self) -> SourceHealth:
        """Report on the last fetch. No network call here."""
        if not self._fetch_called:
            return SourceHealth(
                status="down",
                detail="No fetch performed yet",
                failed=list(self.feeds),
            )

        failed = [k for k, v in self._last_health.items() if v in ("error", "unknown_feed")]
        empty = [k for k, v in self._last_health.items() if v == 0 and k not in failed]
        succeeded = [k for k, v in self._last_health.items() if isinstance(v, int) and v > 0]

        if not self._last_articles:
            status = "down"
        elif failed or empty:
            status = "degraded"
        else:
            status = "ok"

        detail = f"Sensors: {len(self._last_articles)} articles from {len(succeeded)} feeds"
        if failed:
            detail += f", failed: {', '.join(failed)}"
        if empty:
            detail += f", empty: {', '.join(empty)}"

        return SourceHealth(
            status=status,
            detail=detail,
            succeeded=succeeded,
            failed=failed + empty,
        )
