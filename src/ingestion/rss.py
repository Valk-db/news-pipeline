"""RSS feed ingestion for tier-1 news sources."""

import feedparser
import httpx
from typing import List, Dict, Optional
from datetime import datetime, timezone
from src.utils.trafilatura_extract import extract_article, compute_url_hash, compute_content_hash
from src.utils.ner import extract_entities
from src.utils.ingest_stats import STATS
from src.schema.models import RawArticle, SourceTier
from src.shared.config import get_settings
import asyncio


TIER1_FEEDS = {
    # 2026-09-21: publisher returns 403 (AP) / 401 (Reuters) to GitHub runners; no official RSS
    "bbc": {
        "name": "BBC News",
        "domain": "bbc.com",
        "tier": SourceTier.TIER1,
        "feeds": [
            "http://feeds.bbci.co.uk/news/world/rss.xml",
            "http://feeds.bbci.co.uk/news/uk/rss.xml",
            "http://feeds.bbci.co.uk/news/politics/rss.xml",
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
    "npr": {
        "name": "NPR",
        "domain": "npr.org",
        "tier": SourceTier.TIER1,
        "feeds": [
            "https://feeds.npr.org/1001/rss.xml",  # News
            "https://feeds.npr.org/1003/rss.xml",  # World
            "https://feeds.npr.org/1014/rss.xml",  # Politics
        ],
    },
}


async def fetch_feed(client: httpx.AsyncClient, feed_url: str, timeout: int = 30, source_key: str = "", max_retries: int = 3, retry_delay: float = 5.0) -> Optional[feedparser.FeedParserDict]:
    """Fetch and parse a single RSS feed with retry logic."""
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
            if attempt < max_retries - 1:
                print(f"Failed to fetch {feed_url} (attempt {attempt + 1}/{max_retries}): {e}, retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
            else:
                print(f"Failed to fetch {feed_url} after {max_retries} attempts: {e}")
                STATS.record(source_key, f"feed_failed:http_{e.response.status_code}")
                return None
        except asyncio.TimeoutError:
            if attempt < max_retries - 1:
                print(f"Timeout fetching {feed_url} (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
            else:
                print(f"Timeout fetching {feed_url} after {max_retries} attempts")
                STATS.record(source_key, "feed_failed:timeout")
                return None
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"Failed to fetch {feed_url} (attempt {attempt + 1}/{max_retries}): {e}, retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
            else:
                print(f"Failed to fetch {feed_url} after {max_retries} attempts: {e}")
                STATS.record(source_key, f"feed_failed:error_{type(e).__name__}")
                return None


async def process_feed_entry(
    entry: feedparser.FeedParserDict,
    source_info: dict,
    seen_urls: set,
    source_key: str,
) -> Optional[RawArticle]:
    """Process a single feed entry into a RawArticle."""
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

    # Extract entities
    entities = extract_entities(body_text, top_n=3)

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


async def ingest_rss_feeds(max_per_feed: int = 50) -> List[RawArticle]:
    """Ingest all configured RSS feeds."""
    settings = get_settings()
    timeout = settings.rss_fetch_timeout
    seen_urls = set()
    articles = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        for source_key, source_info in TIER1_FEEDS.items():
            for feed_url in source_info["feeds"]:
                feed = await fetch_feed(client, feed_url, timeout=timeout, source_key=source_key)
                if not feed or not feed.entries:
                    continue

                for entry in feed.entries[:max_per_feed]:
                    article = await process_feed_entry(entry, source_info, seen_urls, source_key)
                    if article:
                        articles.append(article)
                        seen_urls.add(article.url_hash)

    return articles