"""Source tier definitions and tier-1 gate logic."""

import uuid
from collections import defaultdict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import (
    ReportingUnit, Story, StoryUnitLink, SourceTier,
    Claim, ClaimType, CanonicalEntity
)
from typing import List, Optional, Tuple
from src.shared.config import get_settings


async def recompute_story_counters(session: AsyncSession, story_ids: List[uuid.UUID]) -> None:
    """
    Recompute story tier1_unit_count, tier2_unit_count, tier3_unit_count, tier4_unit_count, and distinct_owners
    from current StoryUnitLinks.

    This should be called whenever StoryUnitLinks change (attach/detach/merge).

    Note: distinct_owners is computed from tier1_owner_groups to match
    the tier-1 gate logic which requires >=2 distinct tier-1 owners.
    """
    if not story_ids:
        return

    # Get all linked units for these stories with their tier and tier1_owner_groups
    stmt = (
        select(StoryUnitLink.story_id, ReportingUnit.source_tiers, ReportingUnit.tier1_owner_groups)
        .join(ReportingUnit, ReportingUnit.id == StoryUnitLink.unit_id)
        .where(StoryUnitLink.story_id.in_(story_ids))
    )
    result = await session.execute(stmt)
    rows = result.all()

    # Aggregate by story_id
    story_tiers: dict[uuid.UUID, dict[str, int]] = {}
    story_tier1_owners: dict[uuid.UUID, set[str]] = {}

    for story_id, source_tiers, tier1_owner_groups in rows:
        if story_id not in story_tiers:
            story_tiers[story_id] = {}
            story_tier1_owners[story_id] = set()

        # Sum up tier counts
        for tier_str, count in (source_tiers or {}).items():
            story_tiers[story_id][tier_str] = story_tiers[story_id].get(tier_str, 0) + count

        # Collect tier-1 owners (owners that have tier-1 units)
        for owner, _ in (tier1_owner_groups or {}).items():
            story_tier1_owners[story_id].add(owner)

    # Now compute tier1_unit_count, tier2_unit_count, tier3_unit_count, tier4_unit_count, and distinct_owners for each story
    # Batch fetch all stories in one query
    stmt_stories = select(Story).where(Story.id.in_(story_ids))
    result_stories = await session.execute(stmt_stories)
    stories = {s.id: s for s in result_stories.scalars().all()}

    for story_id in story_ids:
        tiers = story_tiers.get(story_id, {})
        tier1_owners = story_tier1_owners.get(story_id, set())

        tier1_count = tiers.get("tier1", 0)
        tier2_count = tiers.get("tier2", 0)
        tier3_count = tiers.get("tier3", 0)
        tier4_count = tiers.get("tier4", 0)
        distinct_owners = len(tier1_owners)  # distinct tier-1 owners

        story = stories.get(story_id)
        if story:
            story.tier1_unit_count = tier1_count
            story.tier2_unit_count = tier2_count
            story.tier3_unit_count = tier3_count
            story.tier4_unit_count = tier4_count
            story.distinct_owners = distinct_owners

    await session.flush()


# Tier-1 sources (verified editorial standards)
# Note: classify_source_tier() below is only consulted by scripts/print_tier_reference.py;
# the live RSS ingestion path (src/ingestion/rss.py TIER1_FEEDS) assigns tier directly and
# does not read this set. Kept in sync with TIER1_FEEDS anyway to avoid the two drifting.
# apnews.com/reuters.com are listed as tier-1 by classification but currently have no
# active ingestion path (RSS pulled 2026-09-21, GDELT disabled) -- see AGENT_TASKS.md.
TIER1_DOMAINS = {
    "bbc.com",
    "theguardian.com",
    "npr.org",
    "dw.com",
    "france24.com",
    "aljazeera.com",
    "euronews.com",
    "pbs.org",
    "apnews.com",
    "reuters.com",
}

# Tier-2 sources (national/regional reputable outlets)
# Keep ONLY the sources that are actually enabled in source_registry.py
# and have verified working RSS feeds.
# See AGENT_TASKS.md P3.1 for the cleanup rationale.
TIER2_DOMAINS = {
    # Verified working RSS (enabled in source_registry)
    "nytimes.com",
    "washingtonpost.com",
    "wsj.com",
    "ft.com",
    "economist.com",
    "foreignpolicy.com",
    "foreignaffairs.com",
    "csis.org",
    "who.int",
    "latimes.com",
    "chicagotribune.com",
    "bostonglobe.com",
    "sfgate.com",
    "seattletimes.com",
    "denverpost.com",
    "miamiherald.com",
    "ajc.com",
    "houstonchronicle.com",
    "dallasnews.com",
    "phillyinquirer.com",
    "startribune.com",
    "oregonlive.com",
    "dispatch.com",
    "tennessean.com",
    "courier-journal.com",
    "cincinnati.com",
    "indystar.com",
    "jsonline.com",
    "freep.com",
    "azcentral.com",
    "reviewjournal.com",
    "rgj.com",
    "cjonline.com",
    "statesman.com",
    "pressherald.com",
    "burlingtonfreepress.com",
    "dailycamera.com",
    "coloradoan.com",
    "journalnow.com",
    "greensboro.com",
    "fayobserver.com",
    "citizen-times.com",
    "postandcourier.com",
    "thestate.com",
    "tallahassee.com",
    "news-press.com",
    "naplesnews.com",
    "pnj.com",
    "tcpalm.com",
    "floridatoday.com",
    "tampabay.com",
    "orlandosentinel.com",
    "sun-sentinel.com",
    "palmbeachpost.com",
    "tcpanews.com",
    "kansascity.com",
    "stltoday.com",
    "columbiatribune.com",
    "springfieldnewssun.com",
    "daytondailynews.com",
    "wichitaeagle.com",
    "kansas.com",
    "omaha.com",
    "journalstar.com",
    "rapidcityjournal.com",
    "argusleader.com",
    "siouxcityjournal.com",
    "thegazette.com",
    "qctimes.com",
    "desmoinesregister.com",
    "waterloocedarfallscourier.com",
    "globegazette.com",
    "messengernews.net",
    "carrollspaper.com",
    "dailyjournal.net",
    "timesdaily.com",
    "decaturdaily.com",
    "annistonstar.com",
    "gadsdentimes.com",
    "dothaneagle.com",
    "opelikaauburnnews.com",
    "tuscaloosanews.com",
    "montgomeryadvertiser.com",
    "timesrecordnews.com",
    "wacotrib.com",
    "tylerpaper.com",
    "longviewnewsjournal.com",
    # Disabled (no working RSS) - commented out:
    # "brookings.edu",
    # "chathamhouse.org",
    # "un.org",
}

# Tier-3 sources (social, forums, unverified)
TIER3_DOMAINS = {
    "reddit.com",
    "twitter.com",
    "x.com",
    "bsky.social",
    "threads.net",
    "mastodon.social",
    "facebook.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "instagram.com",
}

# Tier-4 sources (niche, hyperlocal, experimental)
TIER4_DOMAINS = {
    "substack.com",
    "medium.com",
    "ghost.io",
    "letterboxd.com",
    "patreon.com",
    "buymeacoffee.com",
    "ko-fi.com",
    "gumroad.com",
    "itch.io",
    "hackernoon.com",
    "dev.to",
    "hashnode.com",
    "towardsdatascience.com",
    "betterprogramming.pub",
    "levelup.gitconnected.com",
    "javascript.plainenglish.io",
    "python.plainenglish.io",
    "uxplanet.org",
    "uxdesign.cc",
    "uxcollective.com",
    "blog.prototypr.io",
    "uxmatters.com",
    "smashingmagazine.com",
    "alistapart.com",
    "css-tricks.com",
    "web.dev",
    "developers.google.com",
    "webplatform.news",
    "frontendfoc.us",
    "javascriptweekly.com",
    "react.statuscode.com",
    "nodeweekly.com",
    "golangweekly.com",
    "rustweekly.com",
    "pythonweekly.com",
    "djangoweekly.com",
    "railsweekly.com",
    "elixirweekly.com",
    "postgresweekly.com",
    "dbweekly.com",
    "dataengineeringweekly.com",
    "mlops.community",
    "kdnuggets.com",
    "towardsdatascience.com",
    "machinelearningmastery.com",
    "distill.pub",
    "paperswithcode.com",
    "huggingface.co",
    "wandb.ai",
    "comet.ml",
    "neptune.ai",
    "mlflow.org",
    "dagshub.com",
    "clear.ml",
    "zenml.io",
    "pytorchlightning.ai",
    "lightning.ai",
    "weights-biases.com",
}


def classify_source_tier(domain: str) -> SourceTier:
    """Classify a domain into a source tier."""
    if domain in TIER1_DOMAINS:
        return SourceTier.TIER1
    if domain in TIER2_DOMAINS:
        return SourceTier.TIER2
    if domain in TIER3_DOMAINS:
        return SourceTier.TIER3
    if domain in TIER4_DOMAINS:
        return SourceTier.TIER4
    return SourceTier.TIER3


def evaluate_tier1_gate(tier_owner_pairs: List[tuple[str, str]]) -> Tuple[bool, str]:
    """
    Pure function to evaluate if a story passes the tier-1 gate.

    Gate passes iff:
    - At least 2 units with source_tier == TIER1
    - Those units have ≥2 distinct ownership groups

    Args:
        tier_owner_pairs: List of (source_tier, owner_group) tuples extracted from ReportingUnit rows.
                          source_tier values: "tier1", "tier2", "tier3" (strings from JSON)
                          owner_group values: ownership group names (strings from JSON)

    Returns (should_queue, reason).
    """
    tier1_pairs = [(tier, owner) for tier, owner in tier_owner_pairs if tier == "tier1"]
    if len(tier1_pairs) < 2:
        return False, f"Only {len(tier1_pairs)} tier-1 units (need ≥2)"

    owners = {owner for _, owner in tier1_pairs}
    if len(owners) < 2:
        return False, f"Tier-1 units from only {len(owners)} owner(s) (need ≥2 distinct)"

    return True, "Gate passed"


async def apply_tier1_gate(
    session: AsyncSession,
    story_ids: Optional[List[uuid.UUID]] = None
) -> dict:
    """
    Apply the tier-1 gate: stories need ≥2 tier-1 reporting units
    from distinct ownership groups.

    If story_ids provided (not None), only evaluate those stories (including BLOCKED).
    Otherwise evaluate all PENDING stories.
    """
    # Build query - if story_ids is not None, get those (any status), else get PENDING
    if story_ids is not None:
        stmt = select(Story).where(Story.id.in_(story_ids))
    else:
        stmt = select(Story).where(Story.status == Story.Status.PENDING)
    result = await session.execute(stmt)
    stories = result.scalars().all()

    queued = 0
    blocked = 0

    # First, recompute all counters from current StoryUnitLinks
    story_id_list = [s.id for s in stories]
    if story_id_list:
        await recompute_story_counters(session, story_id_list)

    # Batch fetch all units for all stories in a single query
    story_units: defaultdict[uuid.UUID, list] = defaultdict(list)
    if story_id_list:
        stmt = (
            select(Story, ReportingUnit)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .join(Story, Story.id == StoryUnitLink.story_id)
            .where(Story.id.in_(story_id_list))
        )
        result = await session.execute(stmt)
        for story, unit in result.all():
            story_units[story.id].append(unit)

    for story in stories:
        # Get linked reporting units for gate evaluation (from batched query)
        units = story_units.get(story.id, [])

        # Convert real ReportingUnit rows to (tier, owner_group) tuples for pure function
        # tier1_owner_groups is already a JSON dict like {"AP": 1, "BBC": 2} computed in units.py
        tier_owner_pairs = []

        for u in units:
            # source_tiers is JSON like {"tier1": 2, "tier2": 1}
            source_tiers = u.source_tiers or {}
            for tier, count in source_tiers.items():
                if tier == "tier1":
                    tier1_owner_groups = u.tier1_owner_groups or {}
                    for owner, owner_count in tier1_owner_groups.items():
                        for _ in range(owner_count):
                            tier_owner_pairs.append((tier, owner))
                # tier2 doesn't matter for the gate

        # Use pure function for gate decision
        should_queue, reason = evaluate_tier1_gate(tier_owner_pairs)

        # Update story status and gate_reason (counters already updated by recompute_story_counters)
        if should_queue:
            # Gate passes: keep PENDING (awaiting curator approval)
            story.status = Story.Status.PENDING
            story.gate_reason = f"Passed gate: {story.tier1_unit_count} tier-1 units, {story.distinct_owners} distinct owners"
            queued += 1
        else:
            story.status = Story.Status.BLOCKED
            story.gate_reason = f"Blocked: {reason}"
            blocked += 1

    await session.commit()
    return {"queued": queued, "blocked": blocked}


async def compute_harm_level(session: AsyncSession, story: "Story") -> str:
    """Return 'high' iff the story has an ALLEGATION claim AND at least one
    entity in story.primary_entities resolves to a CanonicalEntity with
    entity_type == 'PERSON'. Otherwise returns 'low'.

    Note: primary_entities stores canonical entity IDs (UUIDs) as strings.
    """
    # Check if story has any ALLEGATION claims
    claim_stmt = select(Claim).where(
        Claim.story_id == story.id,
        Claim.claim_type == ClaimType.ALLEGATION,
    )
    claim_result = await session.execute(claim_stmt)
    has_allegation = claim_result.scalar_one_or_none() is not None

    if not has_allegation:
        return "low"

    # Check if any primary_entity is a PERSON
    # primary_entities stores canonical IDs as strings (JSON array of UUID strings)
    if not story.primary_entities:
        return "low"

    # Convert string IDs to UUID for the .in_() filter
    canonical_ids = [uuid.UUID(eid) for eid in story.primary_entities if eid]
    if not canonical_ids:
        return "low"

    entity_stmt = select(CanonicalEntity).where(
        CanonicalEntity.id.in_(canonical_ids),
        CanonicalEntity.entity_type == "PERSON",
    )
    entity_result = await session.execute(entity_stmt)
    person_entity = entity_result.scalar_one_or_none()

    return "high" if person_entity else "low"


async def compute_virality_signal(session: AsyncSession, story: "Story") -> int:
    """Return max article_count for units on this story where source_tiers
    includes tier3 or tier4. 0 if none.
    """
    stmt = select(ReportingUnit).where(
        ReportingUnit.id.in_(
            select(StoryUnitLink.unit_id).where(StoryUnitLink.story_id == story.id)
        )
    )
    result = await session.execute(stmt)
    units = result.scalars().all()

    max_viral = 0
    for unit in units:
        source_tiers = unit.source_tiers or {}
        if "tier3" in source_tiers or "tier4" in source_tiers:
            max_viral = max(max_viral, unit.article_count or 0)

    return max_viral


async def compute_admission_score(
    session: AsyncSession,
    story: "Story",
    tier1_unit_count: int,
    distinct_owners: int,
) -> tuple[int, dict]:
    """Returns (score 0-100, breakdown dict for gate_reason logging).

    Factors:
    1. source_tier_baseline: 40 if tier1_unit_count >= 2 else 0
    2. corroboration: min(tier1_unit_count * 10, 30)
    3. distinct_owners: min(distinct_owners * 5, 20)
    4. virality_signal: min(virality_signal * 2, 10) -- tier3/4 article count
    5. harm_level penalty: -20 if 'high', else 0 -- raises the bar

    If harm_level == 'high', the required score to pass is increased by 20
    (i.e., need 70 instead of 50). This implements 'scales the bar' not
    'gates alone' per GRAND_PLAN §3.
    """
    # 1. Source tier baseline (matches old boolean gate)
    tier_baseline = 40 if tier1_unit_count >= 2 else 0

    # 2. Corroboration (more tier-1 units = more corroboration)
    corroboration = min(tier1_unit_count * 10, 30)

    # 3. Distinct owners
    owners_score = min(distinct_owners * 5, 20)

    # 4. Virality signal (tier3/4 article count)
    virality = await compute_virality_signal(session, story)
    virality_score = min(virality * 2, 10)

    # 5. Harm level
    harm_level = await compute_harm_level(session, story)
    harm_penalty = -20 if harm_level == "high" else 0

    # Base score
    base_score = tier_baseline + corroboration + owners_score + virality_score
    final_score = max(0, min(100, base_score + harm_penalty))

    # Passing threshold: 50 normally, 70 if harm_level == "high"
    pass_threshold = 70 if harm_level == "high" else 50
    passes = final_score >= pass_threshold

    breakdown = {
        "tier_baseline": tier_baseline,
        "corroboration": corroboration,
        "distinct_owners": owners_score,
        "virality": virality_score,
        "harm_level": harm_level,
        "harm_penalty": harm_penalty,
        "base_score": base_score,
        "final_score": final_score,
        "pass_threshold": pass_threshold,
        "passes": passes,
    }

    return final_score, breakdown


async def apply_dynamic_gate(
    session: AsyncSession,
    story_ids: Optional[List[uuid.UUID]] = None,
) -> dict:
    """Apply the dynamic admission score gate.

    If dynamic_gate_enabled is False, delegates to apply_tier1_gate.
    If enabled, runs compute_admission_score and uses score threshold.
    """
    settings = get_settings()

    if not settings.dynamic_gate_enabled:
        # Shadow mode - just run old gate
        return await apply_tier1_gate(session, story_ids)

    # Build query - if story_ids is not None, get those (any status), else get PENDING
    if story_ids is not None:
        stmt = select(Story).where(Story.id.in_(story_ids))
    else:
        stmt = select(Story).where(Story.status == Story.Status.PENDING)
    result = await session.execute(stmt)
    stories = result.scalars().all()

    queued = 0
    blocked = 0

    # First, recompute all counters from current StoryUnitLinks
    story_id_list = [s.id for s in stories]
    if story_id_list:
        await recompute_story_counters(session, story_id_list)

    # Batch fetch all units for all stories in a single query
    story_units: defaultdict[uuid.UUID, list] = defaultdict(list)
    if story_id_list:
        stmt = (
            select(Story, ReportingUnit)
            .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
            .join(Story, Story.id == StoryUnitLink.story_id)
            .where(Story.id.in_(story_id_list))
        )
        result = await session.execute(stmt)
        for story, unit in result.all():
            story_units[story.id].append(unit)

    for story in stories:
        # Use already-computed counters (from recompute_story_counters)
        tier1_unit_count = story.tier1_unit_count
        distinct_owners = story.distinct_owners

        # Compute admission score
        score, breakdown = await compute_admission_score(
            session, story, tier1_unit_count, distinct_owners
        )

        # Determine pass/fail
        passes = breakdown["passes"]

        # Update story status and gate_reason
        if passes:
            story.status = Story.Status.PENDING
            parts = [f"{k}={v}" for k, v in breakdown.items() if k not in ("passes", "pass_threshold")]
            story.gate_reason = f"Dynamic gate passed (score={score}, threshold={breakdown['pass_threshold']}): " + ", ".join(parts)
            queued += 1
        else:
            story.status = Story.Status.BLOCKED
            parts = [f"{k}={v}" for k, v in breakdown.items() if k not in ("passes", "pass_threshold")]
            story.gate_reason = f"Dynamic gate blocked (score={score}, threshold={breakdown['pass_threshold']}): " + ", ".join(parts)
            blocked += 1

    await session.commit()
    return {"queued": queued, "blocked": blocked}