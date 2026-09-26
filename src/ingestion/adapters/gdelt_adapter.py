"""GDELT adapter wrapping ingest_gdelt() from gdelt.py.

Its (articles, health) return already has succeeded/failed/skipped keys —
map straight into SourceHealth. Preserve the existing
GDELT_TIER1_CRITICAL_DOMAINS check; it currently lives in run.py computing
tier1_critical_down — keep that computation in run.py after calling
health_check(), don't bury it inside the adapter.
"""
from src.ingestion.adapter import SourceHealth
from src.ingestion import gdelt


class GDELTAdapter:
    """Adapter for GDELT ingestion."""

    name = "gdelt"

    def __init__(self):
        self._last_health: dict = {"succeeded": [], "failed": [], "skipped": []}
        self._last_articles: list = []

    async def fetch(self) -> list:
        """Fetch articles from GDELT."""
        articles, health = await gdelt.ingest_gdelt(hours_back=24, max_per_domain=50)
        self._last_articles = articles
        self._last_health = health
        return articles

    async def health_check(self) -> SourceHealth:
        """Return health from the last fetch."""
        succeeded = self._last_health.get("succeeded", [])
        failed = self._last_health.get("failed", [])
        skipped = self._last_health.get("skipped", [])

        if not self._last_articles and not succeeded:
            status = "down"
        elif failed or skipped:
            status = "degraded"
        else:
            status = "ok"

        return SourceHealth(
            status=status,
            detail=f"GDELT: {len(succeeded)} succeeded, {len(failed)} failed, {len(skipped)} skipped",
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
        )