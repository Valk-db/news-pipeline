"""Per-(source, topic) reliability scoring from claim-evidence consensus.

Not fact-check-verdict based -- see AGENT_TASKS.md P3-B for why
FactCheckRecord/CorrectionRecord aren't the source here (they're
unpopulated in production today).
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import (
    Claim, ClaimEvidence, ClaimStance, ReportingUnit, RawArticle,
    StoryTopicGroup, SourceTopicReliability,
)

MIN_SAMPLE_SIZE = 3  # don't write a score off fewer than this many evidence rows


async def compute_source_topic_reliability(session: AsyncSession, lookback_days: int = 90) -> dict:
    results = {"pairs_scored": 0, "claims_considered": 0, "errors": []}
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    stmt = (
        select(
            ClaimEvidence.claim_id, ClaimEvidence.stance, ClaimEvidence.confidence,
            StoryTopicGroup.topic_group_id, RawArticle.source_domain,
        )
        .join(Claim, Claim.id == ClaimEvidence.claim_id)
        .join(StoryTopicGroup, StoryTopicGroup.story_id == Claim.story_id)
        .join(ReportingUnit, ReportingUnit.id == ClaimEvidence.unit_id)
        .join(RawArticle, RawArticle.id == ReportingUnit.representative_article_id)
        .where(Claim.first_seen_at >= cutoff)
    )
    rows = (await session.execute(stmt)).all()

    by_claim: dict = defaultdict(list)
    for claim_id, stance, confidence, topic_group_id, source_domain in rows:
        by_claim[claim_id].append((stance, confidence, topic_group_id, source_domain))

    # pair_stats[(source_domain, topic_group_id)] = {"agree": weighted, "disagree": weighted, "n": count}
    pair_stats: dict = defaultdict(lambda: {"agree": 0.0, "disagree": 0.0, "n": 0})

    for claim_id, evidence in by_claim.items():
        non_neutral = [e for e in evidence if e[0] != ClaimStance.NEUTRAL]
        if len(non_neutral) < 2:
            continue  # no majority to measure agreement against
        results["claims_considered"] += 1
        support_w = sum(c for s, c, _, _ in non_neutral if s == ClaimStance.SUPPORTS)
        dispute_w = sum(c for s, c, _, _ in non_neutral if s == ClaimStance.DISPUTES)
        majority = ClaimStance.SUPPORTS if support_w >= dispute_w else ClaimStance.DISPUTES
        for stance, confidence, topic_group_id, source_domain in non_neutral:
            key = (source_domain, topic_group_id)
            pair_stats[key]["n"] += 1
            if stance == majority:
                pair_stats[key]["agree"] += confidence
            else:
                pair_stats[key]["disagree"] += confidence

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for (source_domain, topic_group_id), stats in pair_stats.items():
        if stats["n"] < MIN_SAMPLE_SIZE:
            continue  # thin-data floor -- no row rather than a noisy one
        total = stats["agree"] + stats["disagree"]
        score = round(100 * stats["agree"] / total) if total else 50

        existing_stmt = select(SourceTopicReliability).where(
            SourceTopicReliability.source_domain == source_domain,
            SourceTopicReliability.topic_group_id == topic_group_id,
            SourceTopicReliability.snapshot_date == today,
        )
        existing = (await session.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            existing.score, existing.sample_size = score, stats["n"]
        else:
            session.add(SourceTopicReliability(
                source_domain=source_domain, topic_group_id=topic_group_id,
                score=score, sample_size=stats["n"], snapshot_date=today,
            ))
        results["pairs_scored"] += 1

    await session.commit()
    return results