"""Reddit ingestion via public RSS feeds (no API credentials required)."""

import asyncio
from datetime import UTC, datetime
from typing import Callable, Awaitable, Optional

import httpx
from bs4 import BeautifulSoup

from src.ingestion.rss import fetch_feed
from src.schema.models import RawArticle, SourceTier
from src.shared.config import get_settings
from src.utils.ingest_stats import STATS
from src.utils.ner import extract_entities_top_n
from src.utils.trafilatura_extract import compute_content_hash, compute_url_hash, extract_article

# Target subreddits for geopolitics/news
TARGET_SUBREDDITS = [
    "worldnews",
    "geopolitics",
    "news",
    "politics",
    "europe",
    "middleeast",
    "china",
    "russia",
    "ukraine",
    "credibledefense",
    "lesscredibledefense",
]

# Reddit throttles anonymous RSS traffic; space subreddit fetches out.
REDDIT_FETCH_DELAY_SECONDS = 3.0

NON_ARTICLE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".gifv", ".mp4", ".webm")


def extract_outbound_url(entry, comments_url: str) -> str | None:
    """Pull the submission's outbound link out of the entry summary HTML.

    Reddit's RSS <link>/entries[i].link is always the comments permalink.
    The real submission URL lives inside a "[link]"-labeled anchor in the
    summary. For self (text) posts, that anchor points back at the comments
    page itself, which we treat as "no outbound URL" and skip.
    """
    summary_html = entry.get("summary", "")
    if not summary_html:
        return None
    soup = BeautifulSoup(summary_html, "html.parser")
    for a in soup.find_all("a"):
        if a.get_text(strip=True) == "[link]":
            href = a.get("href", "")
            if href and href != comments_url:
                return href
            return None
    return None


async def process_entry(entry, source_key: str = "reddit") -> RawArticle | None:
    """Process a single Reddit RSS entry into a RawArticle."""
    settings = get_settings()
    comments_url = entry.get("link", "")
    url = extract_outbound_url(entry, comments_url)
    if not url or url.lower().endswith(NON_ARTICLE_EXTENSIONS):
        return None

    url_hash = compute_url_hash(url)
    STATS.record(source_key, "entries_seen")

    # Extract article body
    body_text, extracted_title = await extract_article(url, source_key=source_key)
    if not body_text:
        # Extraction failed (403, timeout, empty, etc.) - already recorded by extract_article
        return None
    if len(body_text) < 200:  # Actually too short, not an extraction failure
        STATS.record(source_key, "too_short")
        return None

    title = extracted_title or entry.get("title", "").strip()
    if not title:
        return None

    # Parse date
    published_at = None
    if "published_parsed" in entry and entry.published_parsed:
        published_at = datetime(*entry.published_parsed[:6], tzinfo=UTC)
    elif "updated_parsed" in entry and entry.updated_parsed:
        published_at = datetime(*entry.updated_parsed[:6], tzinfo=UTC)

    # Entities (cap driven by settings.top_n_entities) - run in thread to avoid blocking event loop
    entities = await asyncio.to_thread(extract_entities_top_n, body_text, top_n=settings.top_n_entities)

    # Content hash
    content_hash = compute_content_hash(body_text)

    # Tier 3 for Reddit (unverified social)
    art = RawArticle(
        url=url,
        url_hash=url_hash,
        title=title,
        body_text=body_text,
        summary=None,
        source_domain="reddit.com",
        source_tier=SourceTier.TIER3,
        published_at=published_at,
        entities=entities,
        content_hash=content_hash,
    )
    STATS.record(source_key, "ok")
    return art


async def ingest_reddit(
    subreddits: list[str] | None = None,
    limit_per_sub: int = 25,
    time_filter: str = "day",
    known_url_hashes: set[str] | None = None,
    filter_known: Optional[Callable[[set[str]], Awaitable[set[str]]]] = None,
) -> list[RawArticle]:
    """Ingest top submissions from target subreddits via public RSS (no auth).

    Args:
        subreddits: List of subreddit names to fetch from
        limit_per_sub: Maximum entries per subreddit
        time_filter: Time filter for Reddit top feed (day, week, month)
        known_url_hashes: Optional set of URL hashes already known in DB.
        filter_known: Optional async callable(filter_known(hashes: set[str]) -> set[str])
    """
    settings = get_settings()
    timeout = settings.rss_fetch_timeout

    if subreddits is None:
        subreddits = TARGET_SUBREDDITS
    else:
        subreddits = list(subreddits)

    articles = []
    seen_hashes = set()
    headers = {"User-Agent": settings.reddit_user_agent}

    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        for i, sub_name in enumerate(subreddits):
            if i > 0:
                await asyncio.sleep(REDDIT_FETCH_DELAY_SECONDS)

            feed_url = f"https://www.reddit.com/r/{sub_name}/top.rss?t={time_filter}&limit={limit_per_sub}"
            feed = await fetch_feed(client, feed_url, timeout=timeout, source_key=f"reddit.{sub_name}")
            if not feed or not feed.entries:
                STATS.record(f"reddit.{sub_name}", "feed_failed:empty_feed")
                continue

            for entry in feed.entries[:limit_per_sub]:
                comments_url: str = entry.get("link", "")  # type: ignore[assignment]
                url = extract_outbound_url(entry, comments_url)
                if not url or url.lower().endswith(NON_ARTICLE_EXTENSIONS):
                    continue

                url_hash = compute_url_hash(url)

                # Check against known URL hashes from DB (P1-1: dedup before extraction)
                if known_url_hashes and url_hash in known_url_hashes:
                    STATS.record(f"reddit.{sub_name}", "already_known")
                    continue
                if filter_known:
                    known = await filter_known({url_hash})
                    if url_hash in known:
                        STATS.record(f"reddit.{sub_name}", "already_known")
                        continue

                if url_hash in seen_hashes:
                    continue

                article = await process_entry(entry, source_key=f"reddit.{sub_name}")
                if article:
                    articles.append(article)
                    seen_hashes.add(article.url_hash)

    return articles


# TODO: Reddit's Data API commercial-use terms haven't been independently verified
# in this build process. Public RSS carries no stated per-request auth of its own,
# but confirm Reddit's current terms before this output feeds anything that gets
# posted for reach.
