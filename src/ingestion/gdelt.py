"""GDELT ingestion for geopolitics and wire-service coverage.

Primary path: v2 DOC API (artlist) per domain.
Fallback path: legacy v1 GKG GeoJSON API, used only when the DOC API yields
nothing. The v1 endpoint still serves geocoded coverage directly (the v2
/geo/geo endpoint is dead), so fallback articles arrive with lat/lng that we
stash in entities["GEO"].
"""

import httpx
import asyncio
import json
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse
from src.utils.trafilatura_extract import extract_article, compute_url_hash, compute_content_hash
from src.utils.ner import extract_entities_top_n
from src.schema.models import RawArticle, SourceTier
from src.shared.config import get_settings
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


@dataclass
class DomainResult:
    domain: str
    articles: List[RawArticle] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    max_retries: int = 7,
    base_delay: float = 10.0,
) -> httpx.Response:
    """Fetch with exponential backoff retry for rate limits."""
    for attempt in range(max_retries):
        response = await client.get(url, params=params)
        if response.status_code != 429:
            return response

        # Rate limited - exponential backoff with jitter
        delay = base_delay * (2 ** attempt) + random.uniform(0, 2)
        logger.warning("GDELT rate limited (429), attempt %d/%d, waiting %.1fs...", attempt + 1, max_retries, delay)
        await asyncio.sleep(delay)

    # Final attempt without catching 429
    return await client.get(url, params=params)


async def fetch_gdelt_articles(
    domain: str,
    hours_back: int = 24,
    max_records: int = 100,
    throttle_seconds: float = 5.0,
) -> DomainResult:
    """
    Fetch articles from GDELT DOC API for a specific domain.
    Rate limited to 1 request per throttle_seconds.
    Returns DomainResult with ok/error classification.
    """
    settings = get_settings()
    throttle = settings.gdelt_throttle_seconds
    max_retries = settings.gdelt_max_retries
    base_delay = settings.gdelt_base_delay

    # Build query: domain + last 24h + English
    query = f"domain:{domain} language:english"
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(max_records),
        "sort": "datedesc",
    }

    articles: List[RawArticle] = []

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await fetch_with_retry(client, GDELT_API, params, max_retries=max_retries, base_delay=base_delay)
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
                        published_at = datetime.strptime(seendate, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass

                # Entities (cap driven by settings.top_n_entities)
                entities = extract_entities_top_n(body_text, top_n=settings.top_n_entities)

                # Content hash
                content_hash = compute_content_hash(body_text)

                # Determine tier by domain
                tier = SourceTier.TIER1 if domain in TIER1_DOMAINS else SourceTier.TIER2

                art = RawArticle(
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
                )
                articles.append(art)

        except Exception as e:
            logger.error("GDELT fetch failed for %s: %s", domain, e)
            return DomainResult(domain=domain, ok=False, error=str(e))

        await asyncio.sleep(throttle)

    return DomainResult(domain=domain, articles=articles, ok=True)


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
                                published_at = published_at.replace(tzinfo=timezone.utc)
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


async def ingest_gdelt(hours_back: int = 24, max_per_domain: int = 50) -> tuple[List[RawArticle], dict]:
    """Ingest from all configured domains via GDELT. Returns (articles, health)."""
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