"""GDELT ingestion for geopolitics and wire-service coverage.

Primary path: GDELT 2.0 static files, handled in gdelt_static.py. Reading the
published 15 minute files gives us real article URLs, titles and event
geography without depending on the DOC API's rate limits.

Degraded paths, in order, used only when the static files yield nothing:
  1. v2 DOC API (artlist) per domain.
  2. legacy v1 GKG GeoJSON API. The v1 endpoint still serves geocoded coverage
     directly (the v2 /geo/geo endpoint is dead), so fallback articles arrive
     with lat/lng that we stash in entities["GEO"].
"""

import httpx
import asyncio
import json
import logging
import time
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime, UTC
from urllib.parse import urlparse
from src.utils.trafilatura_extract import extract_article, compute_url_hash, compute_content_hash
from src.utils.ner import extract_entities_top_n
from src.schema.models import RawArticle, SourceTier
from src.shared.config import get_settings
from src.shared.database import get_session
from src.ingestion.gdelt_static import ingest_static_file_set
import random


logger = logging.getLogger(__name__)


GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
# Legacy v1 GKG GeoJSON API. Still serves; returns geocoded features directly.
GDELT_GKG_GEOJSON_API = "https://api.gdeltproject.org/api/v1/gkg_geojson"
# GDELT domains - AP and Reuters now have RSS backups, so only use GDELT for BBC/Guardian/NPR
# to reduce rate limit pressure on critical wire services
DOMAIN_FILTERS = ["bbc.com", "theguardian.com", "npr.org"]
GDELT_TIER1_CRITICAL_DOMAINS = set()  # All tier-1 sources now have RSS backups
TIER1_DOMAINS = {"apnews.com", "reuters.com", "bbc.com", "theguardian.com", "npr.org"}
# Broad fallback queries (label, GKG query). Only used when DOC yields nothing.
GKG_FALLBACK_QUERIES = [
    ("conflict", "conflict"),
    ("protest", "protest"),
    ("disaster", "earthquake"),
    ("election", "election"),
    ("political", "government"),
    ("economic", "economy"),
]


# GDELT signals throttling with a short plain-text apology rather than a status
# code alone: "Please limit requests to one every 5 seconds...". Verified live
# 2026-10-02 against api.gdeltproject.org, which serves it with a 429 and no
# content-type; it has also arrived with a 200, where the old status check passed
# it straight to the JSON parse and the run reported a generic error instead of
# backing off. Match the text so both shapes are caught.
THROTTLE_BODY_MARKERS = (
    "please limit requests",
    "please do not hammer",
    "too many requests",
)
# The apology is a sentence or two. Scanning only the head of the body keeps
# article text in a real payload out of the match.
THROTTLE_BODY_SCAN_CHARS = 512
# Backoff base for a body-detected throttle. Deliberately much longer than the
# 429 base (settings.gdelt_base_delay): a body throttle is an active block, and
# hammering during one extends it, so a queued-request backoff is the wrong size.
THROTTLE_BODY_BASE_DELAY = 60.0
# Consecutive throttle signals (body or 429) that open the DOC circuit. Three
# means the block is not decaying on its own and only time clears it.
THROTTLE_CIRCUIT_THRESHOLD = 3
THROTTLE_CIRCUIT_COOLDOWN_SECONDS = 15 * 60
# GDELT republishes every 15 minutes, so a DOC response cannot go stale inside
# that window. Re-fetching earlier is pure rate limit pressure for zero new data.
DOC_CACHE_TTL_SECONDS = 15 * 60


@dataclass
class DomainResult:
    domain: str
    articles: List[RawArticle] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None


def is_throttle_response(response: httpx.Response) -> bool:
    """True when a response body is GDELT's rate-limit apology.

    Runs before any JSON parse, because that is the whole point: the apology is
    not JSON. Payloads that do start with "{" are real DOC results and are never
    scanned, so an article headline about rate limits cannot trip the breaker.
    """
    body = response.text[:THROTTLE_BODY_SCAN_CHARS].strip()
    if not body or body.startswith("{"):
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in THROTTLE_BODY_MARKERS)


class ThrottleCircuitBreaker:
    """Stop calling the DOC API once GDELT has told us to stop several times.

    A throttle block only expires with time, and the static file path is the
    primary feed, so going cold for a quarter hour costs us nothing. Counted in
    consecutive signals: one success clears the streak, so an isolated throttle
    inside a long run never opens the circuit.
    """

    def __init__(
        self,
        threshold: int = THROTTLE_CIRCUIT_THRESHOLD,
        cooldown_seconds: float = THROTTLE_CIRCUIT_COOLDOWN_SECONDS,
    ) -> None:
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.consecutive_throttles = 0
        self.opened_at: Optional[float] = None

    def record_throttle(self) -> None:
        self.consecutive_throttles += 1
        if self.consecutive_throttles >= self.threshold:
            self.opened_at = time.monotonic()
            logger.warning(
                "GDELT DOC circuit opened: %d consecutive throttles, cooling down %.0f minutes",
                self.consecutive_throttles, self.cooldown_seconds / 60,
            )

    def record_success(self) -> None:
        self.consecutive_throttles = 0
        self.opened_at = None

    def cooldown_remaining(self) -> float:
        if self.opened_at is None:
            return 0.0
        remaining = self.cooldown_seconds - (time.monotonic() - self.opened_at)
        return max(remaining, 0.0)

    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if self.cooldown_remaining() <= 0:
            self.opened_at = None
            self.consecutive_throttles = 0
            return False
        return True

    def state(self) -> str:
        if self.is_open():
            return f"open ({self.cooldown_remaining() / 60:.0f} min left)"
        return f"closed ({self.consecutive_throttles}/{self.threshold} throttles)"


class DocResponseCache:
    """In-process TTL cache for DOC API responses.

    Keyed on the normalized query so two callers asking for the same domain and
    window share one request. Single process by design: this runs inside one
    scheduled job, so there is no second process to share with.
    """

    def __init__(self, ttl_seconds: float = DOC_CACHE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._entries: Dict[tuple, tuple] = {}

    @staticmethod
    def make_key(domain: str, hours_back: int, max_records: int) -> tuple:
        return (domain.strip().lower().removeprefix("www."), int(hours_back), int(max_records))

    def get(self, key: tuple) -> Optional[List[RawArticle]]:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, articles = entry
        if time.monotonic() - stored_at >= self.ttl_seconds:
            del self._entries[key]
            return None
        return articles

    def put(self, key: tuple, articles: List[RawArticle]) -> None:
        self._entries[key] = (time.monotonic(), list(articles))

    def clear(self) -> None:
        self._entries.clear()


class GlobalRateLimiter:
    """One shared minimum spacing for every DOC API request.

    GDELT's limit is per IP, not per domain, so a throttle that resets per domain
    - or that only sleeps after a successful call - does not throttle us at all:
    three domains in a row still land as three requests back to back. The lock
    holds across the sleep so concurrent callers space out in turn.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_request: Optional[float] = None

    async def acquire(self, override: Optional[float] = None) -> None:
        interval = get_settings().gdelt_throttle_seconds if override is None else override
        if interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            if self._last_request is not None:
                wait = self._last_request + interval - now
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request = time.monotonic()


DOC_THROTTLE_CIRCUIT = ThrottleCircuitBreaker()
DOC_RESPONSE_CACHE = DocResponseCache()
DOC_RATE_LIMITER = GlobalRateLimiter()


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    max_retries: int = 7,
    base_delay: float = 10.0,
    throttle_seconds: Optional[float] = None,
) -> httpx.Response:
    """Fetch with exponential backoff retry for rate limits.

    A throttle is whichever GDELT sends, the 429 status or the apology text in
    the body, and both retry here. A body throttle gets the long base delay
    because retrying fast inside an active block is what extends it. Every
    attempt, retries included, goes through the shared rate limiter.
    """
    for attempt in range(max_retries + 1):
        await DOC_RATE_LIMITER.acquire(throttle_seconds)
        response = await client.get(url, params=params)

        body_throttled = is_throttle_response(response)
        if response.status_code != 429 and not body_throttled:
            DOC_THROTTLE_CIRCUIT.record_success()
            return response

        if body_throttled:
            DOC_THROTTLE_CIRCUIT.record_throttle()
            delay = THROTTLE_BODY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 2)
            logger.warning(
                "GDELT throttling in the response body (HTTP %d), attempt %d/%d, "
                "waiting %.1fs, circuit %s",
                response.status_code, attempt + 1, max_retries + 1, delay,
                DOC_THROTTLE_CIRCUIT.state(),
            )
        else:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 2)
            logger.warning(
                "GDELT rate limited (429), attempt %d/%d, waiting %.1fs...",
                attempt + 1, max_retries + 1, delay,
            )

        # No sleep after the final attempt: there is no further attempt to wait for.
        if attempt < max_retries:
            if DOC_THROTTLE_CIRCUIT.is_open():
                # The breaker just opened on this response. Sleeping out the rest of
                # the backoff only to be ignored on arrival is the worst of both.
                logger.warning("GDELT DOC circuit opened mid-fetch, not retrying %s", url)
                return response
            await asyncio.sleep(delay)

    return response


async def _articles_from_doc_response(
    response: httpx.Response, domain: str, top_n: int
) -> DomainResult:
    """Classify one DOC API response and build RawArticles from it."""
    if is_throttle_response(response):
        return DomainResult(
            domain=domain,
            ok=False,
            error="throttled: GDELT asked us to limit requests in the response body",
        )

    response.raise_for_status()

    # Check for empty response
    if not response.text.strip():
        return DomainResult(domain=domain, ok=False, error="empty response")

    # Validate JSON response (GDELT sometimes returns error text instead of JSON)
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        return DomainResult(domain=domain, ok=False, error=f"non-json response: {content_type}")

    data = response.json()

    # Check if articles key exists - valid JSON with empty/missing articles = genuine "nothing new"
    if not data.get("articles"):
        return DomainResult(domain=domain, articles=[], ok=True)

    articles: List[RawArticle] = []
    for article in data.get("articles", []):
        url = article.get("url", "")
        if not url:
            continue

        url_hash = compute_url_hash(url)

        # Extract body
        body_text, extracted_title = await extract_article(url)
        if not body_text or len(body_text) < 200:
            continue

        title = extracted_title or article.get("title", "").strip()
        if not title:
            continue

        # Parse date
        published_at = None
        seendate = article.get("seendate", "")
        if seendate:
            try:
                # GDELT format: YYYYMMDDHHMMSS
                published_at = datetime.strptime(seendate, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            except ValueError:
                pass

        # Entities (cap driven by settings.top_n_entities)
        entities = extract_entities_top_n(body_text, top_n=top_n)

        # Content hash
        content_hash = compute_content_hash(body_text)

        # Determine tier by domain
        tier = SourceTier.TIER1 if domain in TIER1_DOMAINS else SourceTier.TIER2

        articles.append(RawArticle(
            url=url,
            url_hash=url_hash,
            title=title,
            body_text=body_text,
            summary=article.get("excerpt", "")[:500] if article.get("excerpt") else None,
            source_domain=domain,
            source_tier=tier,
            published_at=published_at,
            entities=entities,
            content_hash=content_hash,
        ))

    return DomainResult(domain=domain, articles=articles, ok=True)


async def fetch_gdelt_articles(
    domain: str,
    hours_back: int = 24,
    max_records: int = 100,
    throttle_seconds: Optional[float] = None,
) -> DomainResult:
    """
    Fetch articles from GDELT DOC API for a specific domain.
    Rate limited to 1 request per throttle_seconds (settings.gdelt_throttle_seconds
    unless overridden); the limit is per-IP, so the spacing is shared globally.
    Returns DomainResult with ok/error classification.
    """
    settings = get_settings()
    max_retries = settings.gdelt_max_retries
    base_delay = settings.gdelt_base_delay

    if DOC_THROTTLE_CIRCUIT.is_open():
        logger.warning(
            "GDELT DOC circuit open, skipping %s (%.0f min left; static files are the primary path)",
            domain, DOC_THROTTLE_CIRCUIT.cooldown_remaining() / 60,
        )
        return DomainResult(domain=domain, ok=False, error="circuit_open")

    # GDELT only republishes every 15 minutes, so a response still inside that
    # window cannot be stale. Serving it from cache saves the request entirely.
    cache_key = DOC_RESPONSE_CACHE.make_key(domain, hours_back, max_records)
    cached = DOC_RESPONSE_CACHE.get(cache_key)
    if cached is not None:
        logger.info("GDELT DOC cache hit for %s (%d articles)", domain, len(cached))
        return DomainResult(domain=domain, articles=list(cached), ok=True)

    # Build query: domain + last 24h + English
    query = f"domain:{domain} language:english"
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(max_records),
        "sort": "datedesc",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await fetch_with_retry(
                client, GDELT_API, params,
                max_retries=max_retries,
                base_delay=base_delay,
                throttle_seconds=throttle_seconds,
            )
            result = await _articles_from_doc_response(
                response, domain, settings.top_n_entities
            )
        except Exception as e:
            logger.error("GDELT fetch failed for %s: %s", domain, e)
            return DomainResult(domain=domain, ok=False, error=str(e))

    # Only successes are cached: a throttle or a parse error must not be replayed.
    if result.ok:
        DOC_RESPONSE_CACHE.put(cache_key, result.articles)
    return result


async def fetch_gkg_geojson_articles(
    queries: Optional[List[tuple]] = None,
    hours_back: int = 24,
    max_per_query: int = 40,
    throttle_seconds: float = 2.0,
) -> DomainResult:
    """
    Fallback ingestion via the legacy v1 GKG GeoJSON API.

    Called only when the DOC API path produces zero articles. Each feature
    already carries coordinates and a location name, so the resulting
    RawArticles get entities["GEO"] = {"lat", "lon", "name"} for free.
    Article bodies are still extracted with trafilatura like the main path,
    so downstream dedup/curation sees the same shape.
    """
    settings = get_settings()
    queries = queries or GKG_FALLBACK_QUERIES
    articles: List[RawArticle] = []
    seen_hashes = set()

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            for label, query in queries:
                params = {
                    "QUERY": query,
                    "TIMESPAN": str(hours_back * 60),
                    "OUTPUTFIELDS": "url,name,tone,lang",
                }
                response = None
                for attempt in range(3):
                    try:
                        response = await client.get(GDELT_GKG_GEOJSON_API, params=params)
                        response.raise_for_status()
                        break
                    except Exception as e:
                        logger.warning(
                            "GKG GeoJSON query %r attempt %d/3 failed: %s", query, attempt + 1, e
                        )
                        response = None
                        if attempt < 2:
                            await asyncio.sleep(2.0 * (attempt + 1))
                if response is None:
                    continue

                try:
                    # The feed occasionally mixes encodings; never die on decode.
                    data = json.loads(response.content.decode("utf-8", errors="replace"))
                except Exception as e:
                    logger.warning("GKG GeoJSON query %r returned unparsable body: %s", query, e)
                    continue

                for feature in (data.get("features") or [])[:max_per_query]:
                    props = feature.get("properties") or {}
                    coords = (feature.get("geometry") or {}).get("coordinates") or []
                    url = (props.get("url") or "").strip()
                    if len(coords) != 2 or not url:
                        continue
                    lon, lat = coords
                    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                        continue

                    url_hash = compute_url_hash(url)
                    if url_hash in seen_hashes:
                        continue

                    body_text, extracted_title = await extract_article(url)
                    if not body_text or len(body_text) < 200:
                        continue
                    location_name = (props.get("name") or "Unknown location").strip() or "Unknown location"
                    title = extracted_title or f"News report from {location_name}"
                    if not title:
                        continue

                    published_at = None
                    pub = props.get("urlpubtimedate") or ""
                    if pub:
                        try:
                            published_at = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                            if published_at.tzinfo is None:
                                # GKG sometimes omits the offset; assume UTC like the DOC path.
                                published_at = published_at.replace(tzinfo=UTC)
                        except ValueError:
                            pass

                    entities = extract_entities_top_n(body_text, top_n=settings.top_n_entities)
                    try:
                        tone = float(props.get("urltone") or 0)
                    except (TypeError, ValueError):
                        tone = 0.0
                    entities["GEO"] = {
                        "lat": lat,
                        "lon": lon,
                        "name": location_name,
                        "tone": tone,
                        "lang": props.get("urllangcode"),
                        "fallback": "gkg-geojson",
                    }

                    domain = urlparse(url).netloc or "unknown"
                    tier = SourceTier.TIER1 if domain in TIER1_DOMAINS else SourceTier.TIER2

                    articles.append(RawArticle(
                        url=url,
                        url_hash=url_hash,
                        title=title,
                        body_text=body_text,
                        summary=body_text[:500],
                        source_domain=domain,
                        source_tier=tier,
                        published_at=published_at,
                        entities=entities,
                        content_hash=compute_content_hash(body_text),
                    ))
                    seen_hashes.add(url_hash)

                await asyncio.sleep(throttle_seconds)

        except Exception as e:
            logger.error("GKG GeoJSON fallback failed: %s", e)
            return DomainResult(domain="gkg-geojson", ok=False, error=str(e))

    ok = True
    error = None
    if not articles:
        ok = False
        error = "no articles extracted from GKG GeoJSON"
    return DomainResult(domain="gkg-geojson", articles=articles, ok=ok, error=error)


async def fetch_static_articles(
    max_articles_per_file: int = 500,
    known_url_hashes: Optional[set] = None,
) -> DomainResult:
    """
    Primary GDELT path: read the latest 2.0 static file set.

    Returns DomainResult so callers keep one shape. Cap is the latest file set
    only, gdelt_static never pages through history. Known url hashes are read
    from the database so extraction is skipped for rows we already have.
    """
    if known_url_hashes is None:
        known_url_hashes = set()
        try:
            from sqlalchemy import select
            from datetime import timedelta

            cutoff = datetime.now(UTC) - timedelta(days=30)
            async with get_session() as session:
                result = await session.execute(
                    select(RawArticle.url_hash).where(RawArticle.fetched_at >= cutoff)
                )
                known_url_hashes = set(result.scalars().all())
        except Exception as e:
            # No database, or the dedup query failed. Parsing still works, the
            # pipeline's own dedup will catch repeats.
            logger.warning("GDELT static: could not preload url hashes: %s", e)

    static = await ingest_static_file_set(
        max_articles_per_file=max_articles_per_file,
        known_url_hashes=known_url_hashes,
    )

    label = "gdelt-static"
    if static.skipped_no_geo:
        logger.info("GDELT static: %d rows skipped for missing geography", static.skipped_no_geo)
    if static.soft_skips:
        logger.info("GDELT static: soft skipped %s", ", ".join(static.soft_skips))

    return DomainResult(
        domain=label,
        articles=static.articles,
        ok=static.ok,
        error=static.error,
    )


async def ingest_gdelt(hours_back: int = 24, max_per_domain: int = 50) -> tuple[List[RawArticle], dict]:
    """DOC API ingestion per domain, with the v1 GKG GeoJSON path as its fallback.

    This is the degraded path. ingest_gdelt_with_static() calls it only when
    the static files produce nothing.
    """
    settings = get_settings()
    all_articles: List[RawArticle] = []
    seen_hashes = set()
    results: List[DomainResult] = []
    failures = 0

    for domain in DOMAIN_FILTERS:
        if failures >= settings.gdelt_circuit_breaker_threshold:
            results.append(DomainResult(domain=domain, ok=False, error="circuit_open"))
            continue

        result = await fetch_gdelt_articles(domain, hours_back, max_per_domain)
        results.append(result)
        if not result.ok:
            failures += 1

        for art in result.articles:
            if art.url_hash not in seen_hashes:
                all_articles.append(art)
                seen_hashes.add(art.url_hash)

    # Emergency fallback: DOC API gave us nothing at all. Sweep the legacy
    # v1 GKG GeoJSON API for broad, geocoded coverage so the pipeline still
    # eats today.
    fallback_used = False
    fallback_count = 0
    if not all_articles:
        logger.warning("GDELT DOC API produced zero articles; trying v1 GKG GeoJSON fallback")
        fb = await fetch_gkg_geojson_articles(hours_back=hours_back)
        results.append(fb)
        fallback_used = fb.ok
        for art in fb.articles:
            if art.url_hash not in seen_hashes:
                all_articles.append(art)
                seen_hashes.add(art.url_hash)
        fallback_count = len(fb.articles)

    health = {
        "succeeded": [r.domain for r in results if r.ok],
        "failed": [r.domain for r in results if not r.ok and r.error != "circuit_open"],
        "skipped": [r.domain for r in results if r.error == "circuit_open"],
        "fallback_used": fallback_used,
        "fallback_count": fallback_count,
    }
    return all_articles, health


async def ingest_gdelt_with_static(
    hours_back: int = 24,
    max_per_domain: int = 50,
    max_static_articles: int = 500,
    known_url_hashes: Optional[set] = None,
) -> tuple[List[RawArticle], dict]:
    """Ingest GDELT static files, falling back to the DOC API when empty.

    Static files are the main feed. They are not subject to the DOC API rate
    limits and carry article URLs, titles and event geography. Only when they
    yield nothing do we log the degraded line and call ingest_gdelt(), which
    runs the DOC API and then the v1 GKG GeoJSON path.
    """
    static_result = await fetch_static_articles(
        max_articles_per_file=max_static_articles,
        known_url_hashes=known_url_hashes,
    )

    if static_result.articles:
        health = {
            "succeeded": [static_result.domain] if static_result.ok else [],
            "failed": [] if static_result.ok else [static_result.domain],
            "skipped": [],
            "static_count": len(static_result.articles),
            "fallback_used": False,
            "fallback_count": 0,
        }
        return static_result.articles, health

    logger.warning("GDELT static files unavailable, falling back to DOC API (degraded)")

    articles, health = await ingest_gdelt(
        hours_back=hours_back, max_per_domain=max_per_domain
    )
    health["static_count"] = 0
    return articles, health


async def verify_sources() -> Dict[str, int]:
    """
    One-off verification: check that AP and Reuters actually return articles.
    Run once before seeding tier-1 sources.
    """
    results = {}
    for domain in ["apnews.com", "reuters.com"]:
        result = await fetch_gdelt_articles(domain, hours_back=24, max_records=10, throttle_seconds=0)
        results[domain] = len(result.articles)
        logger.info("GDELT %s: %d articles in last 24h", domain, len(result.articles))
    return results