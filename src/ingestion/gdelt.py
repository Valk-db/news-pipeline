"""GDELT DOC API ingestion for geopolitics and wire-service coverage."""

import httpx
import asyncio
from typing import List, Dict, Optional
from datetime import datetime, timezone, timedelta
from src.utils.trafilatura_extract import extract_article, compute_url_hash, compute_content_hash
from src.utils.ner import extract_entities
from src.schema.models import RawArticle, SourceTier
from src.shared.config import get_settings
import re
import random


GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
DOMAIN_FILTERS = ["apnews.com", "reuters.com", "bbc.com", "theguardian.com", "npr.org"]


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    max_retries: int = 5,
    base_delay: float = 5.0,
) -> httpx.Response:
    """Fetch with exponential backoff retry for rate limits."""
    for attempt in range(max_retries):
        response = await client.get(url, params=params)
        if response.status_code != 429:
            return response

        # Rate limited - exponential backoff with jitter
        delay = base_delay * (2 ** attempt) + random.uniform(0, 2)
        print(f"GDELT rate limited (429), attempt {attempt + 1}/{max_retries}, waiting {delay:.1f}s...")
        await asyncio.sleep(delay)

    # Final attempt without catching 429
    return await client.get(url, params=params)


async def fetch_gdelt_articles(
    domain: str,
    hours_back: int = 24,
    max_records: int = 100,
    throttle_seconds: float = 5.0,
) -> List[RawArticle]:
    """
    Fetch articles from GDELT DOC API for a specific domain.
    Rate limited to 1 request per 5 seconds.
    """
    settings = get_settings()
    throttle = settings.gdelt_throttle_seconds

    # Build query: domain + last 24h + English
    query = f"domain:{domain} language:english"
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(max_records),
        "sort": "datedesc",
    }

    articles = []

    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await fetch_with_retry(client, GDELT_API, params)
            response.raise_for_status()

            # Check for empty response
            if not response.text.strip():
                print(f"GDELT empty response for {domain}")
                return articles

            # Validate JSON response (GDELT sometimes returns error text instead of JSON)
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type:
                print(f"GDELT non-JSON response for {domain}: {content_type} - {response.text[:200]}")
                return articles

            data = response.json()

            # Check if articles key exists
            if not data.get("articles"):
                print(f"GDELT no articles found for {domain}")
                return articles

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

                # Entities
                entities = extract_entities(body_text, top_n=3)

                # Content hash
                content_hash = compute_content_hash(body_text)

                # Determine tier by domain
                tier = SourceTier.TIER1 if domain in {"apnews.com", "reuters.com", "bbc.com", "theguardian.com", "npr.org"} else SourceTier.TIER2

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
            print(f"GDELT fetch failed for {domain}: {e}")

        # Throttle between domains
        await asyncio.sleep(throttle)

    return articles


async def ingest_gdelt(hours_back: int = 24, max_per_domain: int = 50) -> List[RawArticle]:
    """Ingest from all configured domains via GDELT."""
    all_articles = []
    seen_hashes = set()

    for domain in DOMAIN_FILTERS:
        articles = await fetch_gdelt_articles(domain, hours_back, max_per_domain)
        for art in articles:
            if art.url_hash not in seen_hashes:
                all_articles.append(art)
                seen_hashes.add(art.url_hash)

    return all_articles


async def verify_sources() -> Dict[str, int]:
    """
    One-off verification: check that AP and Reuters actually return articles.
    Run once before seeding tier-1 sources.
    """
    results = {}
    for domain in ["apnews.com", "reuters.com"]:
        articles = await fetch_gdelt_articles(domain, hours_back=24, max_records=10, throttle_seconds=0)
        results[domain] = len(articles)
        print(f"GDELT {domain}: {len(articles)} articles in last 24h")
    return results