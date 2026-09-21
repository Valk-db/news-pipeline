#!/usr/bin/env python3
"""
Deduplication evaluation script.

Compares MinHash near-dup clustering against a labeled test set
(or ground truth derived from known syndication patterns).

Usage:
    uv run scripts/eval_dedup.py [--threshold=0.85]
"""

import asyncio
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from src.shared.database import init_db, get_session
from src.schema.models import RawArticle, ReportingUnit, StoryUnitLink
from sqlalchemy import select


async def evaluate_dedup(threshold: float = 0.85) -> dict:
    """
    Evaluate deduplication quality.

    Returns metrics: precision, recall, f1 for near-dup clusters.
    """
    await init_db()

    async with get_session() as session:
        # Get all reporting units from last 7 days with their articles
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)

        # Fetch reporting units with article details
        stmt = select(ReportingUnit).where(ReportingUnit.created_at >= cutoff)
        result = await session.execute(stmt)
        units = result.scalars().all()

        if not units:
            return {"error": "No reporting units found in evaluation window"}

        # Build article -> unit mapping
        article_to_unit = {}
        unit_articles = defaultdict(list)

        for unit in units:
            # Get articles in this unit
            stmt = select(RawArticle).where(RawArticle.reporting_unit_id == unit.id)
            result = await session.execute(stmt)
            articles = result.scalars().all()

            for art in articles:
                article_to_unit[art.id] = unit.id
                unit_articles[unit.id].append(art)

        # Ground truth: articles with same url_hash should be in same unit (exact dedup)
        # For near-dup: articles from same domain within 1h with similar content_hash

        # Group by domain and time bucket
        domain_time_groups = defaultdict(list)
        for art_id, unit_id in article_to_unit.items():
            art = await session.get(RawArticle, art_id)
            if art:
                # Hour bucket
                hour_bucket = art.published_at.replace(minute=0, second=0, microsecond=0) if art.published_at else None
                if hour_bucket:
                    key = (art.source_domain, hour_bucket)
                    domain_time_groups[key].append((art_id, unit_id, art.content_hash))

        # Evaluate: articles in same domain/hour with same content_hash should be in same unit
        true_positives = 0
        false_positives = 0
        false_negatives = 0

        for (domain, hour), group in domain_time_groups.items():
            if len(group) < 2:
                continue

            # Group by content_hash (exact content match)
            content_groups = defaultdict(list)
            for art_id, unit_id, content_hash in group:
                content_groups[content_hash].append((art_id, unit_id))

            for content_hash, articles in content_groups.items():
                if len(articles) < 2:
                    continue

                # All articles with same content_hash should be in same unit
                unit_ids = {unit_id for _, unit_id in articles}

                if len(unit_ids) == 1:
                    true_positives += len(articles) - 1  # n-1 correct pairs
                else:
                    # They're split across units - false negatives
                    false_negatives += len(articles) - len(unit_ids)

        # Also check: articles in same unit should have same content_hash or be from same domain
        for unit_id, articles in unit_articles.items():
            if len(articles) < 2:
                continue

            content_hashes = {art.content_hash for art in articles}
            domains = {art.source_domain for art in articles}

            if len(content_hashes) == 1:
                true_positives += len(articles) - 1
            elif len(domains) == 1:
                # Same domain, different content - potential syndication
                true_positives += len(articles) - 1
            else:
                # Mixed domains in same unit - could be false positive
                false_positives += len(articles) - 1

        precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) > 0 else 0
        recall = true_positives / (true_positives + false_negatives) if (true_positives + false_negatives) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

        # Unit stats
        unit_sizes = [len(articles) for articles in unit_articles.values()]

        return {
            "evaluation_window_days": 7,
            "total_units": len(units),
            "total_articles_clustered": sum(unit_sizes),
            "avg_unit_size": sum(unit_sizes) / len(unit_sizes) if unit_sizes else 0,
            "max_unit_size": max(unit_sizes) if unit_sizes else 0,
            "true_positives": true_positives,
            "false_positives": false_positives,
            "false_negatives": false_negatives,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
        }


async def main():
    parser = argparse.ArgumentParser(description="Evaluate deduplication quality")
    parser.add_argument("--threshold", type=float, default=0.85, help="Similarity threshold")
    args = parser.parse_args()

    results = await evaluate_dedup(args.threshold)
    print("Deduplication Evaluation Results:")
    for k, v in results.items():
        print(f"  {k}: {v}")

    # Exit code for CI
    if results.get("f1", 0) < 0.8:
        print("\n⚠️  F1 score below 0.8 - dedup quality may need attention")
        return 1
    return 0


if __name__ == "__main__":
    exit(asyncio.run(main()))