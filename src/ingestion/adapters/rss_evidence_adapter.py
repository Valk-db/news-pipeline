"""Adapter for the RSS evidence locker.

rss_evidence.py owns the work: polite conditional feed polling, full article
extraction, content hashing, Merkle stamping, and the canonical-URL join to
GDELT radar rows. This adapter only owns the adapter contract -- open a session,
call run_evidence_refresh, hand back RawArticle objects, and report on the last
fetch from health_check() rather than making a fresh network call.

The articles this returns have already been persisted by run_evidence_refresh,
which flushes them so the Merkle stamp and the entity edges have ids to hang
off. run.py's normal ingest path therefore sees them as already-present rows and
skips them on the url_hash dedupe, which is what keeps a default run
behaviourally identical to before.

That dedupe is also why the language step lives here and not in run.py: an
article nobody translates is an article whose detected_language stays NULL
forever, because the only code that would have set it is skipped for exactly
these rows. Doing it inside the same get_session() block means the language
fields are still dirty on rows this session owns, so they are flushed with the
insert instead of being set on a detached object after the commit.
"""

import asyncio
import logging

from src.enrichment.translation import translate_articles
from src.ingestion import rss_evidence
from src.ingestion.adapter import SourceHealth
from src.shared.database import get_session

logger = logging.getLogger(__name__)


class RssEvidenceAdapter:
    """Adapter for the tier-1 RSS evidence feeds."""

    name = "rss_evidence"

    def __init__(self, feeds: list[dict] | None = None):
        self.feeds = feeds if feeds is not None else rss_evidence.EVIDENCE_FEEDS
        self._last_articles: list = []
        self._last_result: dict = {}
        self._fetch_called = False

    async def fetch(self) -> list:
        """Run one evidence refresh. Returns the RawArticle objects persisted."""
        async with get_session() as session:
            result = await rss_evidence.run_evidence_refresh(session, feeds=self.feeds)
            articles = list(result.get("_articles") or [])
            if articles:
                # Detection is offline and translation is best effort, so neither
                # can be allowed to cost us the articles this session is holding.
                try:
                    await asyncio.to_thread(translate_articles, articles)
                except Exception as exc:
                    logger.warning(
                        "RSS evidence: language step failed, rows persist untranslated: %s",
                        exc,
                    )
        self._last_articles = articles
        self._last_result = result
        self._fetch_called = True
        logger.info(
            "RSS evidence: %d articles, %d stamped, %d GDELT links, sinks=%s",
            result.get("articles_new", 0),
            result.get("stamped", 0),
            result.get("gdelt_links", 0),
            result.get("sinks"),
        )
        return articles

    async def health_check(self) -> SourceHealth:
        """Report on the last fetch. No network call here."""
        if not self._fetch_called:
            return SourceHealth(
                status="down",
                detail="No fetch performed yet",
                failed=[f["key"] for f in self.feeds],
            )

        result = self._last_result
        polled = list(result.get("feeds_polled") or [])
        failed = list(result.get("feeds_failed") or [])
        skipped = list(result.get("feeds_skipped") or [])
        deferred = list(result.get("feeds_deferred_by_cap") or [])
        new_articles = int(result.get("articles_new") or 0)

        if failed or new_articles == 0:
            status = "degraded"
        else:
            status = "ok"

        sinks = result.get("sinks") or {}
        detail = f"RSS evidence: {new_articles} new articles from {len(polled)} polled feeds"
        if skipped:
            detail += f", not due: {', '.join(skipped)}"
        if failed:
            detail += f", failed: {', '.join(failed)}"
        if deferred:
            detail += f", deferred by cap: {', '.join(deferred)}"
        missing = [name for name, status_value in sinks.items() if status_value != "ok"]
        if missing:
            detail += f", sinks missing tables: {', '.join(missing)}"

        return SourceHealth(
            status=status,
            detail=detail,
            succeeded=polled,
            failed=failed,
            skipped=skipped,
        )