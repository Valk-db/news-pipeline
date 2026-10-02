"""RSS feed ingestion for tier-1 and tier-2 news sources.

The single source of truth for all sources (all tiers) is
`src.ingestion.source_registry` — see `get_enabled_sources_by_tier()`.

Production ingestion always uses source_registry.
"""

import feedparser
import httpx
import logging
from typing import List, Optional, Callable, Awaitable
from datetime import datetime, timezone
from src.utils.trafilatura_extract import extract_article, compute_url_hash, compute_content_hash
from src.utils.ner import extract_entities_top_n
from src.utils.ingest_stats import STATS
from src.ingestion.feed_health import active as active_feed_health, record_poll
from src.schema.models import RawArticle
from src.shared.config import get_settings
import asyncio


logger = logging.getLogger(__name__)


async def fetch_feed(client: httpx.AsyncClient, feed_url: str, timeout: int = 30, source_key: str = "") -> Optional[feedparser.FeedParserDict]:
    """Fetch and parse a single RSS feed with retry logic.

    Retries on 5xx, 429, timeouts, and network errors.
    Other 4xx (401/403/404) are NOT retried - recorded after ONE attempt.
    On 429, honors Retry-After header (capped at 60s).
    """
    settings = get_settings()
    max_retries = settings.rss_max_retries
    retry_delay = settings.rss_retry_delay

    # Every terminal exit below records what this poll saw, so a feed that keeps
    # returning nothing is reported dead after DEAD_AFTER_EMPTY_POLLS runs
    # instead of vanishing quietly. The detail is the same stat key the health
    # report already exposes, so the dead report says *why*, not just *that*.
    def _record(entries: int, detail: str) -> None:
        record_poll(
            active_feed_health(),
            feed_url,
            entries,
            source_key=source_key,
            detail=detail,
        )

    for attempt in range(max_retries):
        try:
            response = await client.get(feed_url, timeout=timeout, follow_redirects=True)
            response.raise_for_status()
            feed = feedparser.parse(response.text)
            # I-P1-4: raise_for_status() proves 2xx, not "is a feed". Validate
            # the parsed payload before recording feed_ok: a 200 Cloudflare
            # interstitial or HTML error page must not count as a healthy feed.
            version = getattr(feed, "version", "") or ""
            entries = getattr(feed, "entries", None) or []
            bozo = bool(getattr(feed, "bozo", False))
            if not version and not entries:
                logger.warning(
                    "Feed %s returned 200 but parsed as no feed (bozo=%s); recording feed_failed",
                    feed_url,
                    bozo,
                )
                STATS.record(source_key, "feed_failed:not_a_feed")
                _record(0, "not_a_feed")
                return None
            STATS.record(source_key, "feed_ok")
            if bozo:
                # Recognized feed (or entries) but malformed XML; still usable,
                # recorded separately so tier-1 health can see the wobble.
                STATS.record(source_key, "feed_bozo")
            # entries, not articles: a feed yielding 50 entries whose pages all
            # 403 is a healthy feed that ingests nothing, and only the entries
            # count can tell those two situations apart.
            if not entries:
                # A well-formed feed with nothing in it: the fetch worked, there
                # is simply nothing to report, and the health report should say
                # that rather than call it a failure.
                _record(0, "0_entries")
            else:
                _record(len(entries), "ok_bozo" if bozo else "ok")
            return feed
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # Retry only on 5xx or 429
            should_retry = status >= 500 or status == 429
            if should_retry and attempt < max_retries - 1:
                # P1-6: Honor Retry-After header on 429, cap at 60s
                wait_time = retry_delay
                if status == 429:
                    retry_after = e.response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait_time = min(int(retry_after), 60)
                        except ValueError:
                            # Retry-After might be HTTP-date, fall back to default
                            pass
                logger.warning("Failed to fetch %s (attempt %d/%d): %s, retrying in %ds...", feed_url, attempt + 1, max_retries, e, wait_time)
                await asyncio.sleep(wait_time)
            else:
                logger.error("Failed to fetch %s after %d attempt(s): %s", feed_url, attempt + 1, e)
                STATS.record(source_key, f"feed_failed:http_{status}")
                _record(0, f"http_{status}")
                return None
        except httpx.TimeoutException:
            if attempt < max_retries - 1:
                logger.warning("Timeout fetching %s (attempt %d/%d), retrying in %ds...", feed_url, attempt + 1, max_retries, retry_delay)
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Timeout fetching %s after %d attempts", feed_url, max_retries)
                STATS.record(source_key, "feed_failed:timeout")
                _record(0, "timeout")
                return None
        except Exception as e:
            # Network errors (connection refused, DNS, etc.) retry
            if attempt < max_retries - 1:
                logger.warning("Failed to fetch %s (attempt %d/%d): %s, retrying in %ds...", feed_url, attempt + 1, max_retries, e, retry_delay)
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Failed to fetch %s after %d attempts: %s", feed_url, max_retries, e)
                STATS.record(source_key, f"feed_failed:error_{type(e).__name__}")
                _record(0, f"error_{type(e).__name__}")
                return None


async def process_feed_entry(
    entry: feedparser.FeedParserDict,
    source_info: dict,
    source_key: str,
) -> Optional[RawArticle]:
    """Process a single feed entry into a RawArticle.

    Note: URL deduplication is handled by the caller (_bounded_process) which
    reserves url_hash in seen_urls under a lock before calling this function.
    This function does not check seen_urls.
    """
    settings = get_settings()
    url = entry.get("link", "")
    if not url:
        return None

    url_hash = compute_url_hash(url)  # type: ignore[arg-type]

    title = entry.get("title", "")
    if isinstance(title, str):
        title = title.strip()
    if not title:
        return None

    # Parse published date
    published_at = None
    if "published_parsed" in entry and entry.published_parsed:
        published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)  # type: ignore[arg-type]
    elif "updated_parsed" in entry and entry.updated_parsed:
        published_at = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)  # type: ignore[arg-type]

    STATS.record(source_key, "entries_seen")

    # Extract article body
    body_text, extracted_title = await extract_article(url, source_key=source_key)  # type: ignore[arg-type]
    if not body_text:
        # Extraction failed (403, timeout, empty, etc.) - already recorded by extract_article
        return None
    if len(body_text) < 200:  # Actually too short, not an extraction failure
        STATS.record(source_key, "too_short")
        return None

    # Use extracted title if better
    if extracted_title and len(extracted_title) > len(title):
        title = extracted_title

    # Extract entities (cap driven by settings.top_n_entities) - run in thread to avoid blocking event loop
    entities = await asyncio.to_thread(extract_entities_top_n, body_text, top_n=settings.top_n_entities)

    # Compute content hash for exact dedup
    content_hash = compute_content_hash(body_text)

    # Build RawArticle
    article = RawArticle(
        url=url,
        url_hash=url_hash,
        title=title,
        body_text=body_text,
        summary=(entry.get("summary", "")[:500] if entry.get("summary") else None),  # type: ignore[index]
        source_domain=source_info["domain"],
        source_tier=source_info["tier"],
        published_at=published_at,
        entities=entities,
        content_hash=content_hash,
    )

    STATS.record(source_key, "ok")
    return article


async def ingest_rss_feeds(
    sources: dict,
    max_per_feed: int = 50,
    known_url_hashes: set[str] | None = None,
    filter_known: Optional[Callable[[set[str]], Awaitable[set[str]]]] = None,
) -> List[RawArticle]:
    """Ingest all configured RSS feeds.

    Args:
        max_per_feed: Maximum articles per feed
        sources: Dict of SourceConfig objects from source_registry (required).
        known_url_hashes: Optional set of URL hashes already known in DB.
                          If provided, entries with these hashes are skipped before extraction.
        filter_known: Optional async callable(filter_known(hashes: set[str]) -> set[str])
                      that returns the subset of hashes already in DB. Used when
                      known_url_hashes is not provided upfront. Allows testing without DB.
    """
    settings = get_settings()
    timeout = settings.rss_fetch_timeout
    seen_urls = set()
    articles = []

    # P1-5: Per-domain circuit breaker state (reset each run)
    # domain -> {"attempts": int, "ok": int, "circuit_open": bool}
    domain_circuit_state: dict[str, dict] = {}

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
        fetch_sem = asyncio.Semaphore(settings.rss_fetch_concurrency)
        logger.info(
            "RSS fetch: %d feed(s), concurrency=%d, timeout=%ss",
            len(feed_tasks),
            settings.rss_fetch_concurrency,
            timeout,
        )

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

            domain = source_info_dict["domain"]

            # Initialize circuit state for this domain
            if domain not in domain_circuit_state:
                domain_circuit_state[domain] = {"attempts": 0, "ok": 0, "circuit_open": False}

            local_articles = []
            for entry in feed.entries[:max_per_feed]:
                url = entry.get("link", "")
                url_hash = compute_url_hash(url) if url else None

                # Reserve URL in seen_urls before extraction (dedup lock)
                async with seen_lock:
                    if not url or url_hash in seen_urls:
                        continue

                    # P0-4: Record entries_in_feed for every entry with a URL, BEFORE already_known checks
                    STATS.record(source_key, "entries_in_feed")

                    # Check against known URL hashes from DB (P1-1: dedup before extraction)
                    if known_url_hashes and url_hash in known_url_hashes:
                        STATS.record(source_key, "already_known")
                        continue
                    if filter_known and url_hash:
                        known = await filter_known({url_hash})
                        if url_hash in known:
                            STATS.record(source_key, "already_known")
                            continue

                    # P1-5: Check circuit breaker for this domain
                    state = domain_circuit_state[domain]
                    if state["circuit_open"]:
                        STATS.record(source_key, "circuit_open")
                        continue

                    seen_urls.add(url_hash)

                async with extract_sem:
                    article = await process_feed_entry(entry, source_info_dict, source_key)

                    # P1-5: Update circuit breaker state after extraction attempt
                    state = domain_circuit_state[domain]
                    state["attempts"] += 1
                    if article:
                        state["ok"] += 1

                    # Check if circuit should open: >=8 attempts and success rate < 10%
                    if state["attempts"] >= 8 and state["ok"] / state["attempts"] < 0.10:
                        if not state["circuit_open"]:
                            # First time opening - record trip as its own stat
                            state["circuit_open"] = True
                            STATS.record(source_key, "circuit_tripped")
                            logger.warning(
                                "Circuit breaker OPEN for domain %s after %d attempts, %d ok (%.1f%% success rate)",
                                domain, state["attempts"], state["ok"], state["ok"] / state["attempts"] * 100
                            )
                        # Already open, subsequent skips are recorded as circuit_open (above)

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