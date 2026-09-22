"""Reddit ingestion via public RSS feeds (no API credentials required)."""

import asyncio
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup

from src.ingestion.rss import fetch_feed
from src.schema.models import RawArticle, SourceTier
from src.shared.config import get_settings
from src.utils.ingest_stats import STATS
from src.utils.ner import extract_entities
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
    comments_url = entry.get("link", "")
    url = extract_outbound_url(entry, comments_url)
    if not url or url.lower().endswith(NON_ARTICLE_EXTENSIONS):
        return None

    url_hash = compute_url_hash(url)
    STATS.record(source_key, "entries_seen")

    # Extract article body
    body_text, extracted_title = await extract_article(url, source_key=source_key)
    if not body_text or len(body_text) < 200:
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

    # Entities
    entities = extract_entities(body_text, top_n=3)

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
    time_filter: str = "day"
) -> list[RawArticle]:
    """Ingest top submissions from target subreddits via public RSS (no auth)."""
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

            # Reddit's top.rss feed is pre-sorted by score (descending), so taking
            # the first `limit_per_sub` entries acts as an implicit quality filter.
            # Old PRAW code used `submission.score >= 10`; RSS exposes no score field.
            feed_url = f"https://www.reddit.com/r/{sub_name}/top.rss?t={time_filter}&limit={limit_per_sub}"
            feed = await fetch_feed(client, feed_url, timeout=timeout, source_key="reddit")
            if not feed or not feed.entries:
                continue

            for entry in feed.entries[:limit_per_sub]:
                article = await process_entry(entry, source_key="reddit")
                if article and article.url_hash not in seen_hashes:
                    articles.append(article)
                    seen_hashes.add(article.url_hash)

    return articles


# TODO: Reddit's Data API commercial-use terms haven't been independently verified
# in this build process. Public RSS carries no stated per-request auth of its own,
# but confirm Reddit's current terms before this output feeds anything that gets
# posted for reach.
