"""Build reporting units by clustering near-duplicate articles."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import RawArticle, ReportingUnit, SourceTier
from src.utils.minhash_utils import (
    shingle_text,
    cluster_articles_by_containment,
)
from src.shared.config import get_settings
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import uuid


OWNERSHIP_GROUPS = {
    # Wire services
    "apnews.com": "AP",
    "reuters.com": "Reuters",
    "afp.com": "AFP",
    "pa.media": "PA Media",

    # US TV rollups
    "sinclair.com": "Sinclair",
    "nexstar.tv": "Nexstar",
    "gray.tv": "Gray Television",
    "tegna.com": "Tegna",

    # Newspaper chains
    "gannett.com": "Gannett",
    "hearst.com": "Hearst",
    "advance.net": "Advance Publications",
    "mclatchy.com": "McClatchy",
    "tribpub.com": "Tribune Publishing",

    # Entertainment (critical for celebrity vertical)
    "variety.com": "Penske",
    "deadline.com": "Penske",
    "hollywoodreporter.com": "Penske",
    "rollingstone.com": "Penske",
    "billboard.com": "Penske",

    # International broadcasters (new tier-1 feeds)
    "dw.com": "DW",
    "france24.com": "France24",

    # Others
    "nytimes.com": "NYT",
    "washingtonpost.com": "WaPo",
    "wsj.com": "WSJ",
    "bbc.com": "BBC",
    "theguardian.com": "Guardian",
    "npr.org": "NPR",
}


def get_owner_group(domain: str) -> str:
    """Map domain to ownership group."""
    # Check exact match first
    if domain in OWNERSHIP_GROUPS:
        return OWNERSHIP_GROUPS[domain]

    # Check subdomain matches
    for known_domain, group in OWNERSHIP_GROUPS.items():
        if domain.endswith("." + known_domain) or domain == known_domain:
            return group

    return "Independent"


async def build_reporting_units(session: AsyncSession) -> int:
    """
    Cluster articles from the last 24h into reporting units.
    Each unit = one reporting event (original + syndications).
    """
    settings = get_settings()
    threshold = settings.containment_threshold

    # Get articles from last 24h that aren't yet clustered
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    stmt = select(RawArticle).where(
        RawArticle.fetched_at >= cutoff,
        RawArticle.reporting_unit_id.is_(None),  # Not yet assigned
    )
    result = await session.execute(stmt)
    articles = result.scalars().all()

    if not articles:
        return 0

    # Group by day bucket (UTC date)
    day_buckets = defaultdict(list)
    for art in articles:
        day = art.published_at or art.fetched_at
        day = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        day_buckets[day].append(art)

    total_units = 0

    for day, day_articles in day_buckets.items():
        # Prepare for clustering: [(article_id, tokens)]
        article_tokens = []
        for art in day_articles:
            tokens = shingle_text(art.body_text or "", k=5)
            article_tokens.append((str(art.id), tokens))

        # Cluster by containment
        clusters = cluster_articles_by_containment(article_tokens, threshold=threshold)

        for cluster in clusters:
            # Find representative (longest body text)
            cluster_articles = [a for a in day_articles if str(a.id) in cluster]
            representative = max(cluster_articles, key=lambda a: len(a.body_text or ""))

            # Count source tiers and owner groups
            tier_counts = defaultdict(int)
            owner_counts = defaultdict(int)
            tier1_owner_counts = defaultdict(int)
            for art in cluster_articles:
                tier_counts[art.source_tier.value] += 1
                owner = get_owner_group(art.source_domain)
                owner_counts[owner] += 1
                if art.source_tier == SourceTier.TIER1:
                    tier1_owner_counts[owner] += 1

            # Create reporting unit
            unit = ReportingUnit(
                day=day,
                representative_article_id=representative.id,
                article_count=len(cluster_articles),
                source_tiers=dict(tier_counts),
                owner_groups=dict(owner_counts),
                tier1_owner_groups=dict(tier1_owner_counts),
            )
            session.add(unit)
            await session.flush()

            # Link articles to unit
            for art in cluster_articles:
                art.reporting_unit_id = unit.id

            total_units += 1

    await session.commit()
    return total_units