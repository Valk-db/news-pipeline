"""Consensus analysis for source reliability - compare source claims against tier-1 baseline."""

import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict
import numpy as np
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import (
    RawArticle, Story, StoryUnitLink, ReportingUnit, SourceTier,
    FactCheckRecord, SourceReliabilitySnapshot,
)
from src.enrichment import get_embedding_service, cosine_similarity
from src.utils.ingest_stats import STATS

logger = logging.getLogger(__name__)


class ConsensusAnalyzer:
    """
    Analyze how well a source's claims align with tier-1 consensus.

    For each story, compute the "consensus embedding" from tier-1 sources,
    then measure each source's alignment via cosine similarity.
    """

    def __init__(self):
        self.embedding_service = get_embedding_service()

    async def analyze_source_consensus(
        self,
        session: AsyncSession,
        source_domain: str,
        days_back: int = 30,
    ) -> Dict[str, Any]:
        """
        Analyze a source's alignment with tier-1 consensus over a period.

        Returns:
            Dict with consensus_alignment score (0-100) and details
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

        # Get tier-1 articles for the same stories as this source
        tier1_articles = await self._get_tier1_articles_for_source(session, source_domain, cutoff)

        if not tier1_articles:
            return {
                "consensus_alignment": 50,  # Neutral if no comparison possible
                "articles_compared": 0,
                "details": "No tier-1 articles found for comparison",
            }

        # Get this source's articles
        source_articles = await self._get_source_articles(session, source_domain, cutoff)

        if not source_articles:
            return {
                "consensus_alignment": 50,
                "articles_compared": 0,
                "details": "No source articles found",
            }

        # Compute consensus embeddings per story from tier-1
        consensus_embeddings = await self._compute_story_consensus_embeddings(
            session, tier1_articles
        )

        # Compare source articles to consensus
        alignments = []
        for article in source_articles:
            # Find which story this article belongs to
            story = await self._get_story_for_article(session, article.id)
            if not story or story.id not in consensus_embeddings:
                continue

            consensus_emb = consensus_embeddings[story.id]

            # Generate embedding for source article
            source_emb_data = await self.embedding_service.generate_embedding(
                article.body_text[:4000] if article.body_text else ""
            )

            similarity = cosine_similarity(source_emb_data, consensus_emb)
            alignments.append(similarity)

        if not alignments:
            return {
                "consensus_alignment": 50,
                "articles_compared": 0,
                "details": "No alignments computed",
            }

        # Average alignment (0-1 -> 0-100)
        avg_alignment = np.mean(alignments) * 100

        return {
            "consensus_alignment": int(round(avg_alignment)),
            "articles_compared": len(alignments),
            "min_alignment": int(round(min(alignments) * 100)),
            "max_alignment": int(round(max(alignments) * 100)),
            "std_alignment": float(np.std(alignments) * 100),
        }

    async def _get_tier1_articles_for_source(
        self,
        session: AsyncSession,
        source_domain: str,
        cutoff: datetime,
    ) -> List[RawArticle]:
        """Get tier-1 articles that cover the same stories as the given source."""
        # Get stories covered by this source
        stmt = (
            select(Story.id)
            .join(StoryUnitLink, StoryUnitLink.story_id == Story.id)
            .join(ReportingUnit, ReportingUnit.id == StoryUnitLink.unit_id)
            .join(RawArticle, RawArticle.id == ReportingUnit.representative_article_id)
            .where(RawArticle.source_domain == source_domain)
            .where(RawArticle.fetched_at >= cutoff)
        )
        result = await session.execute(stmt)
        story_ids = [row[0] for row in result.all()]

        if not story_ids:
            return []

        # Get tier-1 articles for these stories
        stmt = (
            select(RawArticle)
            .join(ReportingUnit, RawArticle.id == ReportingUnit.representative_article_id)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .where(StoryUnitLink.story_id.in_(story_ids))
            .where(RawArticle.source_tier == SourceTier.TIER1)
            .where(RawArticle.fetched_at >= cutoff)
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    async def _get_source_articles(
        self,
        session: AsyncSession,
        source_domain: str,
        cutoff: datetime,
    ) -> List[RawArticle]:
        """Get articles from a specific source."""
        stmt = (
            select(RawArticle)
            .where(RawArticle.source_domain == source_domain)
            .where(RawArticle.fetched_at >= cutoff)
            .where(RawArticle.body_text.isnot(None))
            .where(RawArticle.body_text != "")
        )
        result = await session.execute(stmt)
        return result.scalars().all()

    async def _compute_story_consensus_embeddings(
        self,
        session: AsyncSession,
        tier1_articles: List[RawArticle],
    ) -> Dict[str, List[float]]:
        """Compute consensus embedding for each story from tier-1 articles."""
        # Group articles by story
        story_articles = defaultdict(list)

        for article in tier1_articles:
            # Get story for this article
            stmt = (
                select(Story.id)
                .join(StoryUnitLink, StoryUnitLink.story_id == Story.id)
                .join(ReportingUnit, ReportingUnit.id == StoryUnitLink.unit_id)
                .where(ReportingUnit.representative_article_id == article.id)
            )
            result = await session.execute(stmt)
            story_id = result.scalar_one_or_none()
            if story_id:
                story_articles[story_id].append(article)

        # Compute average embedding per story
        consensus = {}
        for story_id, articles in story_articles.items():
            if len(articles) < 2:
                # Need at least 2 for meaningful consensus
                continue

            texts = [a.body_text[:4000] for a in articles if a.body_text]
            if len(texts) < 2:
                continue

            embeddings = await self.embedding_service.generate_embeddings(texts)
            # Average embeddings
            avg_emb = np.mean(embeddings, axis=0).tolist()
            # Normalize
            norm = np.linalg.norm(avg_emb)
            if norm > 0:
                avg_emb = (np.array(avg_emb) / norm).tolist()
            consensus[story_id] = avg_emb

        return consensus

    async def _get_story_for_article(
        self,
        session: AsyncSession,
        article_id: str,
    ) -> Optional[Story]:
        """Get the story an article belongs to."""
        stmt = (
            select(Story)
            .join(StoryUnitLink, StoryUnitLink.story_id == Story.id)
            .join(ReportingUnit, ReportingUnit.id == StoryUnitLink.unit_id)
            .where(ReportingUnit.representative_article_id == article_id)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def compute_daily_reliability_snapshots(
    session: AsyncSession,
    date: Optional[datetime] = None,
) -> int:
    """
    Compute reliability snapshots for all sources for a given date.

    This is run daily (e.g., via cron) to build time-series of reliability scores.

    Args:
        session: Database session
        date: Date to compute for (defaults to yesterday UTC midnight)

    Returns:
        Number of snapshots created
    """
    if date is None:
        date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Get all sources that have articles in the last 30 days
    cutoff = date - timedelta(days=30)

    stmt = (
        select(RawArticle.source_domain)
        .where(RawArticle.fetched_at >= cutoff)
        .distinct()
    )
    result = await session.execute(stmt)
    source_domains = [row[0] for row in result.all()]

    analyzer = ConsensusAnalyzer()
    snapshots_created = 0

    for domain in source_domains:
        # Check if snapshot already exists
        from sqlalchemy import select as sa_select
        stmt = sa_select(SourceReliabilitySnapshot).where(
            SourceReliabilitySnapshot.source_domain == domain,
            SourceReliabilitySnapshot.snapshot_date == date,
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            continue

        # Get fact-check stats for this source
        fact_check_stats = await _get_fact_check_stats(session, domain, date)

        # Get correction stats
        correction_stats = await _get_correction_stats(session, domain, date)

        # Get consensus alignment
        consensus = await analyzer.analyze_source_consensus(session, domain, days_back=30)

        # Get tier at snapshot
        from src.verification.tiers import classify_source_tier
        tier = classify_source_tier(domain).value

        # Compute composite reliability score
        # Weights: factual=0.4, correction=0.2, consensus=0.3, transparency=0.1
        factual = fact_check_stats["factual_accuracy"]
        correction = max(0, 100 - correction_stats["correction_rate"])  # Invert
        consensus_score = consensus.get("consensus_alignment", 50)
        transparency = _compute_transparency_score(domain)

        reliability_score = int(
            0.4 * factual +
            0.2 * correction +
            0.3 * consensus_score +
            0.1 * transparency
        )

        # Create snapshot
        snapshot = SourceReliabilitySnapshot(
            source_domain=domain,
            snapshot_date=date,
            factual_accuracy=factual,
            correction_rate=correction_stats["correction_rate"],
            consensus_alignment=consensus_score,
            transparency_score=transparency,
            reliability_score=reliability_score,
            total_claims_verified=fact_check_stats["total_verified"],
            claims_true=fact_check_stats["true"],
            claims_false=fact_check_stats["false"],
            claims_mixed=fact_check_stats["mixed"],
            corrections_count=correction_stats["count"],
            articles_sampled=correction_stats["articles_sampled"],
            tier_at_snapshot=tier,
        )
        session.add(snapshot)
        snapshots_created += 1

    await session.commit()
    return snapshots_created


async def _get_fact_check_stats(
    session: AsyncSession,
    source_domain: str,
    snapshot_date: datetime,
) -> Dict[str, Any]:
    """Get fact-check statistics for a source up to snapshot date."""
    stmt = (
        select(FactCheckRecord)
        .where(FactCheckRecord.source_domain == source_domain)
        .where(FactCheckRecord.checked_at <= snapshot_date)
    )
    result = await session.execute(stmt)
    records = result.scalars().all()

    total = len(records)
    if total == 0:
        return {
            "factual_accuracy": 50,  # Neutral if no data
            "total_verified": 0,
            "true": 0,
            "false": 0,
            "mixed": 0,
        }

    true_count = sum(1 for r in records if r.verdict in (FactCheckRecord.Verdict.TRUE, FactCheckRecord.Verdict.MOSTLY_TRUE))
    false_count = sum(1 for r in records if r.verdict in (FactCheckRecord.Verdict.FALSE, FactCheckRecord.Verdict.MOSTLY_FALSE))
    mixed_count = sum(1 for r in records if r.verdict == FactCheckRecord.Verdict.MIXED)

    # Factual accuracy = % of verified claims that are true/mostly_true
    verified = true_count + false_count + mixed_count
    factual_accuracy = int((true_count / verified * 100) if verified > 0 else 50)

    return {
        "factual_accuracy": factual_accuracy,
        "total_verified": verified,
        "true": true_count,
        "false": false_count,
        "mixed": mixed_count,
    }


async def _get_correction_stats(
    session: AsyncSession,
    source_domain: str,
    snapshot_date: datetime,
) -> Dict[str, Any]:
    """Get correction statistics for a source up to snapshot date."""
    stmt = (
        select(CorrectionRecord)
        .where(CorrectionRecord.source_domain == source_domain)
        .where(CorrectionRecord.correction_date <= snapshot_date)
    )
    result = await session.execute(stmt)
    records = result.scalars().all()

    # Count articles sampled (articles with corrections / total articles in period)
    article_ids = set(r.article_id for r in records if r.article_id)

    # Get total articles from this source in period
    cutoff = snapshot_date - timedelta(days=30)
    from sqlalchemy import select as sa_select, func
    stmt = sa_select(func.count(RawArticle.id)).where(
        RawArticle.source_domain == source_domain,
        RawArticle.fetched_at >= cutoff,
    )
    result = await session.execute(stmt)
    total_articles = result.scalar() or 1

    correction_rate = int(len(records) / total_articles * 100)

    return {
        "correction_rate": correction_rate,
        "count": len(records),
        "articles_sampled": len(article_ids),
    }


def _compute_transparency_score(domain: str) -> int:
    """
    Compute transparency score based on known factors.

    Factors (binary):
    - Has corrections policy page
    - Owns mistakes publicly
    - Discloses funding/ownership
    - Has editorial guidelines public
    - Has ombudsman/public editor
    """
    # This would ideally query a transparency database
    # For now, return defaults based on tier/domain knowledge

    transparency_domains = {
        # High transparency
        "apnews.com": 100,
        "reuters.com": 100,
        "bbc.com": 90,
        "theguardian.com": 90,
        "npr.org": 95,
        "nytimes.com": 85,
        "washingtonpost.com": 80,
        "wsj.com": 80,
        "economist.com": 85,
        "ft.com": 80,
        "latimes.com": 75,
        "pbs.org": 85,
        "france24.com": 70,
        "dw.com": 70,
        "aljazeera.com": 60,
        "euronews.com": 65,
    }

    return transparency_domains.get(domain, 40)  # Default moderate transparency


async def detect_corrections(session: AsyncSession) -> int:
    """
    Detect new corrections by comparing current article text with stored versions.

    This runs periodically to find corrections issued by sources.

    Returns:
        Number of new corrections detected
    """
    # Get articles that have been in DB for a while
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    stmt = (
        select(RawArticle)
        .where(RawArticle.fetched_at <= cutoff)
        .where(RawArticle.body_text.isnot(None))
    )
    result = await session.execute(stmt)
    articles = result.scalars().all()

    corrections_found = 0

    for article in articles:
        # Re-fetch current version
        current_text = await _fetch_article_text(article.url)
        if not current_text:
            continue

        # Compare with stored text
        if _texts_differ_significantly(article.body_text, current_text):
            # Check if correction already recorded
            stmt = (
                select(CorrectionRecord)
                .where(CorrectionRecord.article_id == article.id)
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if not existing:
                # Determine severity
                severity = _assess_correction_severity(article.body_text, current_text)

                correction = CorrectionRecord(
                    source_domain=article.source_domain,
                    article_id=article.id,
                    original_text=article.body_text[:5000],
                    corrected_text=current_text[:5000],
                    severity=severity,
                    correction_date=datetime.now(timezone.utc),
                    correction_url=article.url,
                )
                session.add(correction)
                corrections_found += 1

    await session.commit()
    return corrections_found


async def _fetch_article_text(url: str) -> Optional[str]:
    """Fetch current article text from URL."""
    import httpx
    from src.utils.trafilatura_extract import extract_article
    from src.shared.config import get_settings

    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=settings.rss_fetch_timeout) as client:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()

        body_text, _ = await extract_article(url, source_key="correction_check")
        return body_text
    except Exception:
        return None


def _texts_differ_significantly(old: str, new: str) -> bool:
    """Check if two texts differ significantly (not just whitespace/formatting)."""
    # Normalize whitespace
    old_norm = " ".join(old.split())
    new_norm = " ".join(new.split())

    if old_norm == new_norm:
        return False

    # Check for numerical changes - only significant if relative change > 5%
    import re
    old_numbers = [(float(n.rstrip('%')), n.endswith('%')) for n in re.findall(r'\b\d+(?:\.\d+)?%?\b', old_norm)]
    new_numbers = [(float(n.rstrip('%')), n.endswith('%')) for n in re.findall(r'\b\d+(?:\.\d+)?%?\b', new_norm)]

    if len(old_numbers) == len(new_numbers) and len(old_numbers) > 0:
        for (old_val, old_pct), (new_val, new_pct) in zip(old_numbers, new_numbers):
            if old_pct != new_pct:
                return True  # Different units (percentage vs raw)
            if old_val != 0:
                rel_change = abs(new_val - old_val) / old_val
                if rel_change > 0.05:  # 5% relative change threshold
                    return True
            elif new_val != 0:
                return True

    # Check if it's just a minor change (< 5% difference)
    diff_ratio = abs(len(old_norm) - len(new_norm)) / max(len(old_norm), len(new_norm))
    if diff_ratio < 0.05:
        return False

    # Use simple diff
    import difflib
    matcher = difflib.SequenceMatcher(None, old_norm, new_norm)
    similarity = matcher.ratio()

    # Significant if less than 95% similar
    return similarity < 0.95


def _assess_correction_severity(old: str, new: str) -> str:
    """Assess correction severity based on diff."""
    import difflib
    import re

    old_norm = " ".join(old.split())
    new_norm = " ".join(new.split())

    # Check for numerical changes
    old_numbers = [(float(n.rstrip('%')), n.endswith('%')) for n in re.findall(r'\b\d+(?:\.\d+)?%?\b', old_norm)]
    new_numbers = [(float(n.rstrip('%')), n.endswith('%')) for n in re.findall(r'\b\d+(?:\.\d+)?%?\b', new_norm)]
    has_numerical_change = False

    if len(old_numbers) == len(new_numbers) and len(old_numbers) > 0:
        for (old_val, old_pct), (new_val, new_pct) in zip(old_numbers, new_numbers):
            if old_pct != new_pct:
                has_numerical_change = True
                break
            if old_val != 0:
                rel_change = abs(new_val - old_val) / old_val
                if rel_change > 0.05:
                    has_numerical_change = True
                    break
            elif new_val != 0:
                has_numerical_change = True
                break

    # Check text changes
    diff = list(difflib.unified_diff(old_norm.split(), new_norm.split()))
    added = sum(1 for line in diff if line.startswith('+ '))
    removed = sum(1 for line in diff if line.startswith('- '))
    total_changes = added + removed

    # Major: large text changes OR numerical changes with large text changes
    if total_changes > 50:
        return "major"
    elif total_changes > 10 and has_numerical_change:
        return "major"
    elif total_changes > 10 or has_numerical_change:
        return "moderate"
    else:
        return "minor"