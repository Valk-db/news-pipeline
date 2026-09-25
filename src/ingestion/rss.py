"""RSS feed ingestion for tier-1 news sources.

NOTE: TIER1_FEEDS below is DEPRECATED and kept only as a documented fallback.
The single source of truth for all sources (all tiers) is now
`src.ingestion.source_registry` — see `get_enabled_sources_by_tier()`.

Production ingestion always uses source_registry; this dict is only used
if `ingest_rss_feeds(sources=None)` is called explicitly (legacy path).
"""

import feedparser
import httpx
import logging
from typing import List, Optional
from datetime import datetime, timezone
from src.utils.trafilatura_extract import extract_article, compute_url_hash, compute_content_hash
from src.utils.ner import extract_entities_top_n
from src.utils.ingest_stats import STATS
from src.schema.models import RawArticle, SourceTier
from src.shared.config import get_settings
import asyncio


logger = logging.getLogger(__name__)


# DEPRECATED: This dict is NOT used in production (run.py uses source_registry).
# Kept only as a fallback for direct calls to ingest_rss_feeds(sources=None).
# If you update this, ALSO update source_registry.py to keep them in sync.
# See AGENT_TASKS.md P1 for details.
TIER1_FEEDS = {
    # 2026-09-21: publisher returns 403 (AP) / 401 (Reuters) to GitHub runners; no official RSS
    "bbc": {
        "name": "BBC News",
        "domain": "bbc.com",
        "tier": SourceTier.TIER1,
        "feeds": [
            "https://feeds.bbci.co.uk/news/world/rss.xml",
            "https://feeds.bbci.co.uk/news/uk/rss.xml",
            "https://feeds.bbci.co.uk/news/politics/rss.xml",
        ],
    },
    "guardian": {
        "name": "The Guardian",
        "domain": "theguardian.com",
        "tier": SourceTier.TIER1,
        "feeds": [
            "https://www.theguardian.com/world/rss",
            "https://www.theguardian.com/politics/rss",
            "https://www.theguardian.com/us-news/rss",
        ],
    },
    "dw": {
        "name": "Deutsche Welle",
        "domain": "dw.com",
        "tier": SourceTier.TIER1,
        "feeds": [
            "https://rss.dw.com/rdf/rss-en-all",
        ],
    },
    "france24": {
        "name": "France 24",
        "domain": "france24.com",
        "tier": SourceTier.TIER1,
        "feeds": [
            "https://www.france24.com/en/rss",
        ],
    },
    "npr": {
        "name": "NPR",
        "domain": "npr.org",
        "tier": SourceTier.TIER1,
        "feeds": [
            "https://feeds.npr.org/1001/rss.xml",  # News
            "https://feeds.npr.org/1003/rss.xml",  # World
            "https://feeds.npr.org/1014/rss.xml",  # Politics (verified working 2026-09-24)
        ],
    },
    # 2026-09-24: added for tier-1 owner-group diversity (see units.py OWNERSHIP_GROUPS);
    # all three confirmed live and returning valid RSS at addition time.
    "aljazeera": {
        "name": "Al Jazeera English",
        "domain": "aljazeera.com",
        "tier": SourceTier.TIER1,
        "feeds": [
            "https://www.aljazeera.com/xml/rss/all.xml",
        ],
    },
    "euronews": {
        "name": "Euronews",
        "domain": "euronews.com",
        "tier": SourceTier.TIER1,
        "feeds": [
            "https://www.euronews.com/rss",
        ],
    },
    "pbs": {
        "name": "PBS NewsHour",
        "domain": "pbs.org",
        "tier": SourceTier.TIER1,
        "feeds": [
            "https://www.pbs.org/newshour/feeds/rss/headlines",
        ],
    },
}


async def fetch_feed(client: httpx.AsyncClient, feed_url: str, timeout: int = 30, source_key: str = "") -> Optional[feedparser.FeedParserDict]:
    """Fetch and parse a single RSS feed with retry logic.

    Retries on 5xx, 429, timeouts, and network errors.
    Other 4xx (401/403/404) are NOT retried - recorded after ONE attempt.
    """
    settings = get_settings()
    max_retries = settings.rss_max_retries
    retry_delay = settings.rss_retry_delay

    for attempt in range(max_retries):
        try:
            response = await client.get(feed_url, timeout=timeout, follow_redirects=True)
            response.raise_for_status()
            STATS.record(source_key, "feed_ok")
            return feedparser.parse(response.text)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # Retry only on 5xx or 429
            should_retry = status >= 500 or status == 429
            if should_retry and attempt < max_retries - 1:
                logger.warning("Failed to fetch %s (attempt %d/%d): %s, retrying in %ds...", feed_url, attempt + 1, max_retries, e, retry_delay)
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Failed to fetch %s after %d attempt(s): %s", feed_url, attempt + 1, e)
                STATS.record(source_key, f"feed_failed:http_{status}")
                return None
        except httpx.TimeoutException:
            if attempt < max_retries - 1:
                logger.warning("Timeout fetching %s (attempt %d/%d), retrying in %ds...", feed_url, attempt + 1, max_retries, retry_delay)
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Timeout fetching %s after %d attempts", feed_url, max_retries)
                STATS.record(source_key, "feed_failed:timeout")
                return None
        except Exception as e:
            # Network errors (connection refused, DNS, etc.) retry
            if attempt < max_retries - 1:
                logger.warning("Failed to fetch %s (attempt %d/%d): %s, retrying in %ds...", feed_url, attempt + 1, max_retries, e, retry_delay)
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Failed to fetch %s after %d attempts: %s", feed_url, max_retries, e)
                STATS.record(source_key, f"feed_failed:error_{type(e).__name__}")
                return None


async def process_feed_entry(
    entry: feedparser.FeedParserDict,
    source_info: dict,
    seen_urls: set,
    source_key: str,
) -> Optional[RawArticle]:
    """Process a single feed entry into a RawArticle."""
    settings = get_settings()
    url = entry.get("link", "")
    if not url or url in seen_urls:
        return None

    url_hash = compute_url_hash(url)
    if url_hash in seen_urls:
        return None

    title = entry.get("title", "").strip()
    if not title:
        return None

    # Parse published date
    published_at = None
    if "published_parsed" in entry and entry.published_parsed:
        published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    elif "updated_parsed" in entry and entry.updated_parsed:
        published_at = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

    STATS.record(source_key, "entries_seen")

    # Extract article body
    body_text, extracted_title = await extract_article(url, source_key=source_key)
    if not body_text or len(body_text) < 200:  # Too short, likely not a real article
        STATS.record(source_key, "too_short")
        return None

    # Use extracted title if better
    if extracted_title and len(extracted_title) > len(title):
        title = extracted_title

    # Extract entities (cap driven by settings.top_n_entities)
    entities = extract_entities_top_n(body_text, top_n=settings.top_n_entities)

    # Compute content hash for exact dedup
    content_hash = compute_content_hash(body_text)

    # Build RawArticle
    article = RawArticle(
        url=url,
        url_hash=url_hash,
        title=title,
        body_text=body_text,
        summary=entry.get("summary", "")[:500] if entry.get("summary") else None,
        source_domain=source_info["domain"],
        source_tier=source_info["tier"],
        published_at=published_at,
        entities=entities,
        content_hash=content_hash,
    )

    STATS.record(source_key, "ok")
    return article


async def ingest_rss_feeds(max_per_feed: int = 50, sources: dict | None = None) -> List[RawArticle]:
    """Ingest all configured RSS feeds.

    Args:
        max_per_feed: Maximum articles per feed
        sources: Optional dict of SourceConfig objects from source_registry.
                 If None, uses TIER1_FEEDS (backward compatibility).
    """
    settings = get_settings()
    timeout = settings.rss_fetch_timeout
    seen_urls = set()
    articles = []

    # Use provided sources or default to TIER1_FEEDS
    if sources is None:
        sources = TIER1_FEEDS

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Build list of (source_key, feed_url, source_info_dict) tuples
        feed_tasks = []
        for source_key, source_info in sources.items():
            if hasattr(source_info, 'rss_urls'):
                feed_urls = source_info.rss_urls
                domain = source_info.domain
                tier = source_info.tier
                source_name = source_info.name
            else:
                feed_urls = source_info["feeds"]
                domain = source_info["domain"]
                tier = source_info["tier"]
                source_name = source_info["name"]

            source_info_dict = {"domain": domain, "tier": tier, "name": source_name}
            for feed_url in feed_urls:
                feed_tasks.append((source_key, feed_url, source_info_dict))

        # Fetch all feeds concurrently with bounded semaphore
        fetch_sem = asyncio.Semaphore(10)

        async def _bounded_fetch(source_key: str, feed_url: str) -> tuple:
            async with fetch_sem:
                feed = await fetch_feed(client, feed_url, timeout=timeout, source_key=source_key)
                return (source_key, feed_url, feed)

        fetch_futures = [_bounded_fetch(source_key, feed_url) for source_key, feed_url, _ in feed_tasks]
        fetched = await asyncio.gather(*fetch_futures)

        # Process entries with bounded concurrency and dedup lock
        seen_lock = asyncio.Lock()
        extract_sem = asyncio.Semaphore(15)

        async def _bounded_process(source_key: str, feed, source_info_dict: dict) -> List[RawArticle]:
            if not feed or not feed.entries:
                return []

            local_articles = []
            for entry in feed.entries[:max_per_feed]:
                url = entry.get("link", "")
                url_hash = compute_url_hash(url) if url else None

                # Reserve URL in seen_urls before extraction (dedup lock)
                async with seen_lock:
                    if not url or url_hash in seen_urls:
                        continue
                    seen_urls.add(url_hash)

                async with extract_sem:
                    article = await process_feed_entry(entry, source_info_dict, seen_urls, source_key)
                    if article:
                        local_articles.append(article)

            return local_articles

        # Build process tasks
        process_tasks = []
        for (source_key, feed_url, source_info_dict), (_, _, feed) in zip(feed_tasks, fetched):
            if feed and feed.entries:
                process_tasks.append(_bounded_process(source_key, feed, source_info_dict))

        # Run extraction concurrently
        results = await asyncio.gather(*process_tasks)

        # Flatten results
        for article_list in results:
            articles.extend(article_list)

    return articles