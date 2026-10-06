"""Regression tests for the P0 "what counts as corroboration" cluster (2026-10-02).

P0-4: the gate counts tier-1 UNITS, not articles -- one pair per (unit, owner), so a single
      syndicated event can never clear the ">= 2 tier-1 units" bar by itself.
P0-5: viewpoint children are gated on child-exclusive units only -- a slice shares every unit
      with its parent, so parent + 2 children must not triple-count the same evidence.
V-P1-14: the admission score mirrors BOTH halves of the boolean gate (>= 2 tier-1 units AND
      >= 2 distinct owners) -- on independence the score and the gate cannot disagree.

Real SQLite through the shared `db_session` fixture wherever a gate runs end to end; the P0-4
pair-shape test additionally pins the contract at the Corroboration level with no database.
"""

import uuid
from datetime import datetime, UTC
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.schema.models import (
    GateDecision,
    RawArticle,
    ReportingUnit,
    SourceTier,
    Story,
    StoryUnitLink,
)
from src.shared.config import get_settings
from src.verification.corroboration import ArticleEvidence, Corroboration
from src.verification.tiers import apply_dynamic_gate, apply_tier1_gate, evaluate_tier1_gate
from src.verification.units import get_owner_group


def _utcnow():
    return datetime.now(UTC)


async def _make_article(session, *, domain, tier=SourceTier.TIER1):
    slug = uuid.uuid4().hex[:8]
    article = RawArticle(
        id=uuid.uuid4(),
        url=f"https://{domain}/story/{slug}",
        url_hash=uuid.uuid4().hex,
        title=f"Report from {domain} ({slug})",
        body_text=f"Body text for the {slug} report filed by {domain}.",
        source_domain=domain,
        source_tier=tier,
        published_at=_utcnow(),
        entities={"PERSON": ["Jane Doe"], "GPE": ["Lisbon"], "ORG": ["Ministry"]},
    )
    session.add(article)
    await session.flush()
    return article


async def _make_unit(session, articles):
    """One reporting unit the way build_reporting_units() writes it (no wire collapse)."""
    source_tiers, owner_groups, tier1_owner_groups = {}, {}, {}
    for article in articles:
        owner = get_owner_group(article.source_domain)
        tier = article.source_tier.value
        source_tiers[tier] = source_tiers.get(tier, 0) + 1
        owner_groups[owner] = owner_groups.get(owner, 0) + 1
        if article.source_tier is SourceTier.TIER1:
            tier1_owner_groups[owner] = tier1_owner_groups.get(owner, 0) + 1
    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=_utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
        representative_article_id=articles[0].id,
        article_count=len(articles),
        source_tiers=source_tiers,
        owner_groups=owner_groups,
        tier1_owner_groups=tier1_owner_groups,
    )
    session.add(unit)
    await session.flush()
    for article in articles:
        article.reporting_unit_id = unit.id
    return unit


async def _make_story(session, units, *, viewpoint_cluster_id=None):
    story = Story(
        id=uuid.uuid4(),
        day=_utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
        primary_entities=[],
        status=Story.Status.PENDING,
        viewpoint_cluster_id=viewpoint_cluster_id,
    )
    session.add(story)
    await session.flush()
    for unit in units:
        session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
    await session.flush()
    return story


async def _decisions(session, story_id):
    result = await session.execute(
        select(GateDecision)
        .where(GateDecision.story_id == story_id)
        .order_by(GateDecision.decided_at)
    )
    return list(result.scalars().all())


async def _reload(session, story_id):
    return (
        await session.execute(select(Story).where(Story.id == story_id))
    ).scalar_one()


def _evidence(unit_id, domain, owner):
    return ArticleEvidence(
        article_id=uuid.uuid4(),
        unit_id=unit_id,
        url=f"https://{domain}/x/{uuid.uuid4().hex[:8]}",
        source_domain=domain,
        tier="tier1",
        content_hash=None,
        wire_origin=None,
        owner=owner,
    )


# =====================================================================================
# P0-4: one pair per (unit, owner)
# =====================================================================================


def test_tier1_pairs_emit_one_pair_per_unit_owner_not_per_article():
    """Two tier-1 articles inside one reporting unit are one unit's corroboration.

    tier1_pairs() collapses to one (unit_id, owner) per distinct tier-1 owner of the unit, so
    the pair list carries two pairs but one distinct unit id.
    """
    unit_id = uuid.uuid4()
    corroboration = Corroboration(
        by_unit={
            unit_id: (
                _evidence(unit_id, "bbc.com", "BBC"),
                _evidence(unit_id, "theguardian.com", "Guardian"),
            )
        },
        refs=(),
    )
    pairs = corroboration.tier1_pairs([SimpleNamespace(id=unit_id)])
    assert sorted(pairs) == sorted([(unit_id, "BBC"), (unit_id, "Guardian")])
    assert {unit_id for unit_id, _ in pairs} == {unit_id}, "two articles, one unit"


def test_gate_blocks_a_single_syndicated_event():
    """P0-4: one event republished by two tier-1 outlets satisfies ">= 2 articles", not units."""
    unit_id = uuid.uuid4()
    pairs = [(unit_id, "BBC"), (unit_id, "Guardian")]
    should_queue, reason = evaluate_tier1_gate(pairs)
    assert should_queue is False
    assert reason == "Only 1 tier-1 unit (need ≥2)"


def test_gate_passes_two_units_with_one_article_each():
    """The control: two genuinely independent reporting units still pass."""
    pairs = [(uuid.uuid4(), "BBC"), (uuid.uuid4(), "Guardian")]
    should_queue, reason = evaluate_tier1_gate(pairs)
    assert should_queue is True
    assert reason == "Gate passed"


@pytest.mark.asyncio
async def test_one_syndicated_event_cannot_pass_the_gate_end_to_end(db_session):
    """P0-4 through the real gate: one unit holding two tier-1 articles is BLOCKED.

    No content_hash, so no wire collapse is involved -- this is purely the units-vs-articles
    counting. Before the fix the pair list had two entries and the story passed.
    """
    articles = [
        await _make_article(db_session, domain="bbc.com"),
        await _make_article(db_session, domain="theguardian.com"),
    ]
    unit = await _make_unit(db_session, articles)
    story = await _make_story(db_session, [unit])
    await db_session.commit()

    result = await apply_tier1_gate(db_session, story_ids=[story.id])
    assert result == {"queued": 0, "blocked": 1}

    stored = await _reload(db_session, story.id)
    assert stored.status is Story.Status.BLOCKED
    assert "Only 1 tier-1 unit (need ≥2)" in stored.gate_reason
    # The histogram still counts articles per owner -- the unit fix changes the gate's unit
    # walk, not what the ledger records per owner.
    (row,) = await _decisions(db_session, story.id)
    assert row.distinct_owners == 2
    assert row.owner_groups == {"BBC": 1, "Guardian": 1}


# =====================================================================================
# P0-5: viewpoint children gated on child-exclusive units only
# =====================================================================================


@pytest.mark.asyncio
async def test_viewpoint_children_do_not_triple_count_shared_units(db_session):
    """P0-5: parent + 2 children sharing units must not triple-count the evidence.

    cluster_viewpoints() links each slice's units to the child WITHOUT unlinking them from
    the parent. The gate counts each child on child-exclusive units only -- units the slice
    shares with its parent already counted toward the parent's gate. Every unit here is
    shared, so both children are BLOCKED and the three decisions together claim no more
    tier-1 units than actually exist.
    """
    u1 = await _make_unit(db_session, [await _make_article(db_session, domain="bbc.com")])
    u2 = await _make_unit(
        db_session, [await _make_article(db_session, domain="theguardian.com")]
    )
    parent = await _make_story(db_session, [u1, u2])
    child1 = await _make_story(db_session, [u1, u2], viewpoint_cluster_id=parent.id)
    child2 = await _make_story(db_session, [u1], viewpoint_cluster_id=parent.id)
    await db_session.commit()

    result = await apply_tier1_gate(
        db_session, story_ids=[parent.id, child1.id, child2.id]
    )
    assert result == {"queued": 1, "blocked": 2}

    (parent_row,) = await _decisions(db_session, parent.id)
    assert parent_row.passed is True, "the parent's own admission is untouched"
    assert (await _reload(db_session, parent.id)).status is Story.Status.PENDING

    child_rows = []
    for child in (child1, child2):
        (row,) = await _decisions(db_session, child.id)
        child_rows.append(row)
        assert row.passed is False, "a slice must not pass on cloned corroboration"
        assert row.tier1_unit_count == 0
        assert row.distinct_owners == 0
        assert row.owner_groups == {}
        assert row.contributing_articles == []
        stored = await _reload(db_session, child.id)
        assert stored.status is Story.Status.BLOCKED
        assert "Only 0 tier-1 units (need ≥2)" in stored.gate_reason

    # The no-triple-count assertion: one event's two units admitted one story, not three.
    total_claimed = (
        parent_row.tier1_unit_count + sum(r.tier1_unit_count for r in child_rows)
    )
    assert total_claimed == 2


# =====================================================================================
# V-P1-14: the score mirrors both halves of the boolean gate
# =====================================================================================


@pytest.mark.asyncio
async def test_score_cannot_disagree_with_the_gate_on_independence(db_session):
    """V-P1-14: score >= threshold exactly when the boolean gate passes, across unit shapes.

    The middle case is the one that bit: one reporting unit carrying four tier-1 articles from
    four distinct owners. The boolean gate blocks it ("Only 1 tier-1 unit"); a score mirroring
    only the owners half would award 40 + 30 + 20 = 90 and admit it. Both gates must agree.
    """
    settings = get_settings()
    original = settings.dynamic_gate_enabled
    settings.dynamic_gate_enabled = True
    try:
        async def build(domains_per_unit):
            units = []
            for domains in domains_per_unit:
                articles = [await _make_article(db_session, domain=d) for d in domains]
                units.append(await _make_unit(db_session, articles))
            story = await _make_story(db_session, units)
            await db_session.commit()
            return story

        cases = [
            # (units described as article-domain lists, expect_queued)
            ([["bbc.com", "theguardian.com"]], False),          # 1 unit, 2 owners
            ([["bbc.com", "theguardian.com", "npr.org", "cnn.com"]], False),  # 1 unit, 4 owners
            ([["bbc.com"], ["theguardian.com"]], True),          # 2 units, 2 owners
        ]
        for domains_per_unit, expect_queued in cases:
            story = await build(domains_per_unit)

            boolean = await apply_tier1_gate(db_session, story_ids=[story.id])
            dynamic = await apply_dynamic_gate(db_session, [story.id])
            expected = {"queued": 1, "blocked": 0} if expect_queued else {
                "queued": 0, "blocked": 1}
            # apply_tier1_gate returns {"queued", "blocked"}; apply_dynamic_gate adds "errors"
            # (P0 deferred (a): one bad story must not cost the run). The property under test is
            # that the two gates agree, so compare the counters they share and check "errors"
            # separately rather than comparing whole dicts.
            assert boolean == expected, (
                f"{len(domains_per_unit)} unit(s): boolean={boolean} expected={expected}"
            )
            assert {k: dynamic[k] for k in expected} == expected, (
                f"{len(domains_per_unit)} unit(s): boolean={boolean} dynamic={dynamic}"
            )
            assert dynamic["errors"] == 0

            (row,) = [
                r for r in await _decisions(db_session, story.id)
                if r.gate_name == "dynamic"
            ]
            assert row.passed is (row.score >= row.pass_threshold)
            assert row.passed is expect_queued
            assert row.breakdown["tier1_units"] == len(domains_per_unit)
    finally:
        settings.dynamic_gate_enabled = original
