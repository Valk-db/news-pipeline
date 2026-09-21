"""Reddit ingestion via Async PRAW."""

import asyncpraw
from typing import List, Optional
from datetime import datetime, timezone
from src.utils.trafilatura_extract import extract_article, compute_url_hash, compute_content_hash
from src.utils.ner import extract_entities
from src.utils.ingest_stats import STATS
from src.schema.models import RawArticle, SourceTier
from src.shared.config import get_settings


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


def get_reddit_client() -> asyncpraw.Reddit:
    """Create authenticated Async PRAW client."""
    settings = get_settings()
    return asyncpraw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        user_agent=settings.reddit_user_agent,
    )


def is_valid_submission(submission) -> bool:
    """Filter submissions worth processing."""
    # Skip self-posts, removed, stickied
    if submission.is_self or submission.removed_by_category or submission.stickied:
        return False
    # Skip low-score
    if submission.score < 10:
        return False
    # Must have a URL
    if not submission.url or not submission.url.startswith(("http://", "https://")):
        return False
    # Skip media-only (images, videos without article)
    if any(submission.url.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif", ".mp4", ".webm"]):
        return False
    return True


async def process_submission(submission) -> Optional[RawArticle]:
    """Process a Reddit submission into a RawArticle."""
    url = submission.url
    url_hash = compute_url_hash(url)

    STATS.record("reddit", "entries_seen")

    # Extract article body
    body_text, extracted_title = await extract_article(url, source_key="reddit")
    if not body_text or len(body_text) < 200:
        STATS.record("reddit", "too_short")
        return None

    title = extracted_title or submission.title.strip()
    if not title:
        return None

    # Parse date
    published_at = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)

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
        summary=submission.selftext[:500] if submission.selftext else None,
        source_domain="reddit.com",
        source_tier=SourceTier.TIER3,
        published_at=published_at,
        entities=entities,
        content_hash=content_hash,
    )
    STATS.record("reddit", "ok")
    return art


async def ingest_reddit(
    subreddits: Optional[List[str]] = None,
    limit_per_sub: int = 25,
    time_filter: str = "day"
) -> List[RawArticle]:
    """Ingest top submissions from target subreddits using Async PRAW."""
    settings = get_settings()

    # Check if Reddit credentials are available
    if not settings.has_reddit:
        print("Reddit credentials not configured, skipping Reddit ingestion")
        return []

    if subreddits is None:
        subreddits = TARGET_SUBREDDITS
    else:
        subreddits = list(subreddits)

    reddit = get_reddit_client()
    articles = []
    seen_hashes = set()

    try:
        for sub_name in subreddits:
            try:
                subreddit = await reddit.subreddit(sub_name)
                # Async PRAW uses .top() as an async generator
                async for submission in subreddit.top(time_filter=time_filter, limit=limit_per_sub):
                    if not is_valid_submission(submission):
                        continue

                    article = await process_submission(submission)
                    if article and article.url_hash not in seen_hashes:
                        articles.append(article)
                        seen_hashes.add(article.url_hash)

            except Exception as e:
                print(f"Reddit ingestion failed for r/{sub_name}: {e}")

    finally:
        await reddit.close()

    return articles


# TODO: Reddit's Data API commercial-use terms haven't been independently verified
# in this build process. Confirm before PRAW output feeds anything that gets
# posted for reach.