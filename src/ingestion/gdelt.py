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


GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
DOMAIN_FILTERS = ["apnews.com", "reuters.com", "bbc.com", "theguardian.com", "npr.org"]


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
            response = await client.get(GDELT_API, params=params)
            response.raise_for_status()
            data = response.json()

            for article in data.get("articles", []):
                url = article.get("url", "")
                if not url:
                    continue

                url_hash = compute_url_hash(url)

                # Extract body
                body_text, extracted_title = extract_article(url)
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