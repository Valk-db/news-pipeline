"""Tests for stories.gate_decision: the corroboration gate explaining itself.

The gate decides with data it already had -- the tier-1 owner histogram and the articles
behind it -- and before this column that data was only ever recorded as a sentence in
stories.gate_reason. These tests pin four things:

  a) the payload has every key an auditor or query needs, and its passed/reason agree with
     the status and gate_reason written beside it;
  b) contributing_articles is the real chain resolved (story_unit_links -> reporting_units ->
     raw_articles), not a reconstruction;
  c) owner_groups is the tier1_owner_groups histogram, so len(owner_groups) always equals the
     distinct_owners counter stored on the same row;
  d) wire-service copies collapse to one owner and are not counted as independent owners.

Every fixture builds its reporting units through the same arithmetic build_reporting_units()
uses (get_owner_group on each member, tier-1 members only into tier1_owner_groups), so a test
that passes here describes the pipeline's real data rather than a convenient shape.

Real SQLite through the shared db_engine fixture, not mocked sessions: the payload's whole
purpose is what the database stores and what a query returns, and a mocked session proves
neither.
"""

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.schema.models import RawArticle, ReportingUnit, Story, StoryUnitLink, SourceTier
from src.shared.config import get_settings
from src.verification.tiers import (
    GATE_VERSION,
    apply_dynamic_gate,
    apply_tier1_gate,
    build_gate_decision,
    tier1_owner_histogram,
)
from src.verification.units import get_owner_group

REQUIRED_KEYS = {
    "passed",
    "reason",
    "tier1_unit_count",
    "distinct_owners",
    "owner_groups",
    "contributing_articles",
    "score_breakdown",
    "decided_at",
    "gate_version",
}

ENTITIES = {"PERSON": ["Jane Doe"], "GPE": ["Lisbon"], "ORG": ["Ministry"]}


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """A session on the shared in-memory database, as test_counter_mismatch.py uses."""
    async_session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


async def _make_article(
    session: AsyncSession,
    *,
    domain: str,
    tier: SourceTier = SourceTier.TIER1,
    slug: str | None = None,
) -> RawArticle:
    slug = slug or uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    article = RawArticle(
        id=uuid.uuid4(),
        url=f"https://{domain}/story/{slug}",
        url_hash=uuid.uuid4().hex,
        title=f"Report from {domain} ({slug})",
        body_text=f"Body text for the {slug} report filed by {domain}.",
        source_domain=domain,
        source_tier=tier,
        published_at=now,
        entities=ENTITIES,
    )
    session.add(article)
    await session.flush()
    return article


async def _make_unit(
    session: AsyncSession,
    articles: list[RawArticle],
    *,
    representative: RawArticle | None = None,
) -> ReportingUnit:
    """Cluster `articles` into one reporting unit the way build_reporting_units() does.

    The owner arithmetic here is copied from src/verification/units.py on purpose: a fixture
    that hard-coded {"AP": 1} would keep passing if the real collapsing logic changed.
    """
    source_tiers: dict[str, int] = {}
    owner_groups: dict[str, int] = {}
    tier1_owner_groups: dict[str, int] = {}
    for article in articles:
        owner = get_owner_group(article.source_domain)
        source_tiers[article.source_tier.value] = source_tiers.get(article.source_tier.value, 0) + 1
        owner_groups[owner] = owner_groups.get(owner, 0) + 1
        if article.source_tier is SourceTier.TIER1:
            tier1_owner_groups[owner] = tier1_owner_groups.get(owner, 0) + 1

    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        representative_article_id=(representative or articles[0]).id,
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


async def _make_story(session: AsyncSession, units: list[ReportingUnit]) -> Story:
    now = datetime.now(timezone.utc)
    story = Story(
        id=uuid.uuid4(),
        day=now.replace(hour=0, minute=0, second=0, microsecond=0),
        primary_entities=[],
        tier1_unit_count=0,
        tier2_unit_count=0,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=0,
        status=Story.Status.PENDING,
    )
    session.add(story)
    await session.flush()
    for unit in units:
        session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
    await session.flush()
    return story


async def _reload(session: AsyncSession, story_id) -> Story:
    """Read the row back, so assertions are about what the database stored."""
    result = await session.execute(select(Story).where(Story.id == story_id))
    return result.scalar_one()


async def _corroborated_story(session: AsyncSession):
    """Two tier-1 units from two owner groups: the gate passes."""
    unit_bbc = await _make_unit(session, [await _make_article(session, domain="bbc.com")])
    unit_ap = await _make_unit(
        session,
        [
            await _make_article(session, domain="apnews.com"),
            await _make_article(session, domain="live.apnews.com"),
        ],
    )
    return await _make_story(session, [unit_bbc, unit_ap])


# --------------------------------------------------------------------------------------
# (a) shape of the payload
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_decision_has_every_required_key(db_session):
    """A passing story's payload carries every documented key and agrees with its row."""
    story = await _corroborated_story(db_session)
    await db_session.commit()

    result = await apply_tier1_gate(db_session, story_ids=[story.id])
    assert result == {"queued": 1, "blocked": 0}

    stored = await _reload(db_session, story.id)
    decision = stored.gate_decision

    assert REQUIRED_KEYS <= set(decision)
    assert decision["passed"] is True
    assert decision["gate_version"] == GATE_VERSION
    # The two columns are one decision recorded twice: neither may drift from the other.
    assert decision["reason"] == stored.gate_reason
    assert decision["passed"] is (stored.status is Story.Status.PENDING)
    # Counters match the columns the gate decided on.
    assert decision["tier1_unit_count"] == stored.tier1_unit_count == 2
    assert decision["distinct_owners"] == stored.distinct_owners == 2
    assert decision["score_breakdown"] == {
        "tier1_units": 2,
        "distinct_owners": 2,
        "threshold_units": 2,
        "threshold_owners": 2,
    }
    # decided_at is ISO 8601 in UTC, so a payload can be placed in the run that wrote it.
    decided_at = datetime.fromisoformat(decision["decided_at"])
    assert decided_at.tzinfo is not None
    assert decided_at.utcoffset().total_seconds() == 0


@pytest.mark.asyncio
async def test_blocked_story_records_a_false_decision(db_session):
    """One tier-1 unit: blocked, and the payload says so in the queryable fields."""
    unit = await _make_unit(db_session, [await _make_article(db_session, domain="reuters.com")])
    story = await _make_story(db_session, [unit])
    await db_session.commit()

    result = await apply_tier1_gate(db_session, story_ids=[story.id])
    assert result == {"queued": 0, "blocked": 1}

    stored = await _reload(db_session, story.id)
    decision = stored.gate_decision
    assert decision["passed"] is False
    assert decision["passed"] is (stored.status is Story.Status.BLOCKED)
    assert "Only 1 tier-1 units" in decision["reason"]
    assert decision["reason"] == stored.gate_reason
    assert decision["tier1_unit_count"] == 1


@pytest.mark.asyncio
async def test_dynamic_gate_records_the_score_that_decided(db_session):
    """With the flag on, the admission score that decided the story is in the payload."""
    settings = get_settings()
    original = settings.dynamic_gate_enabled
    settings.dynamic_gate_enabled = True
    try:
        story = await _corroborated_story(db_session)
        await db_session.commit()

        result = await apply_dynamic_gate(db_session, story_ids=[story.id])
        assert result == {"queued": 1, "blocked": 0}

        decision = (await _reload(db_session, story.id)).gate_decision
        assert decision["gate_mode"] == "dynamic"
        assert decision["passed"] is True
        assert decision["admission"]["score"] >= decision["admission"]["threshold"]
        # The factors are the same breakdown gate_reason spells out as text.
        assert decision["admission"]["factors"]["tier_baseline"] == 40
        assert "passes" not in decision["admission"]["factors"]
        assert "pass_threshold" not in decision["admission"]["factors"]
    finally:
        settings.dynamic_gate_enabled = original


# --------------------------------------------------------------------------------------
# (b) contributing_articles
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contributing_articles_resolves_the_real_chain(db_session):
    """Every article of every linked unit, with tier and resolved owner, tier-1 first."""
    unit_bbc = await _make_unit(db_session, [await _make_article(db_session, domain="bbc.com")])
    unit_ap = await _make_unit(db_session, [
        await _make_article(db_session, domain="apnews.com"),
        await _make_article(db_session, domain="live.apnews.com", tier=SourceTier.TIER3),
    ])
    story = await _make_story(db_session, [unit_bbc, unit_ap])
    await db_session.commit()

    await apply_tier1_gate(db_session, story_ids=[story.id])
    articles = (await _reload(db_session, story.id)).gate_decision["contributing_articles"]

    assert [a["source_domain"] for a in articles] == [
        "apnews.com",  # tier-1 before tier-3, then alphabetical within a tier
        "bbc.com",
        "live.apnews.com",
    ]
    by_domain = {a["source_domain"]: a for a in articles}
    assert by_domain["apnews.com"] == {
        "url": by_domain["apnews.com"]["url"],
        "source_domain": "apnews.com",
        "owner_group": "AP",
        "tier": "TIER1",
        "unit_id": str(unit_ap.id),
    }
    assert by_domain["apnews.com"]["url"].startswith("https://apnews.com/story/")
    assert by_domain["bbc.com"]["owner_group"] == "BBC"
    assert by_domain["bbc.com"]["tier"] == "TIER1"
    assert by_domain["bbc.com"]["unit_id"] == str(unit_bbc.id)
    # A tier-3 copy is recorded as evidence but is not one of the owners the gate counted.
    assert by_domain["live.apnews.com"]["owner_group"] == "AP"
    assert by_domain["live.apnews.com"]["tier"] == "TIER3"


@pytest.mark.asyncio
async def test_contributing_articles_includes_the_representative_without_a_backlink(db_session):
    """A unit whose members predate reporting_unit_id still names its representative.

    Membership is recorded in raw_articles.reporting_unit_id, so a member row that never got
    it would silently vanish from the audit trail; the representative, matched by id, is the
    floor that keeps a unit from looking as though it cited nothing.
    """
    article = await _make_article(db_session, domain="dw.com")
    unit = await _make_unit(db_session, [article])
    article.reporting_unit_id = None  # legacy member row: unit_id never written
    story = await _make_story(db_session, [unit])
    await db_session.commit()

    await apply_tier1_gate(db_session, story_ids=[story.id])
    articles = (await _reload(db_session, story.id)).gate_decision["contributing_articles"]

    assert [a["url"] for a in articles] == [article.url]
    assert articles[0]["unit_id"] == str(unit.id)
    assert articles[0]["owner_group"] == "DW"


@pytest.mark.asyncio
async def test_contributing_articles_excludes_units_of_other_stories(db_session):
    """The payload is this story's evidence, not the units that happen to share a day."""
    shared_article = await _make_article(db_session, domain="bbc.com")
    unit_mine = await _make_unit(db_session, [shared_article])
    other = await _make_article(db_session, domain="npr.org")
    unit_other = await _make_unit(db_session, [other])
    unit_elsewhere = await _make_unit(db_session, [
        await _make_article(db_session, domain="theguardian.com"),
    ])
    story = await _make_story(db_session, [unit_mine, unit_elsewhere])
    await _make_story(db_session, [unit_other])
    await db_session.commit()

    await apply_tier1_gate(db_session, story_ids=[story.id])
    articles = (await _reload(db_session, story.id)).gate_decision["contributing_articles"]

    domains = {a["source_domain"] for a in articles}
    assert domains == {"bbc.com", "theguardian.com"}
    assert "npr.org" not in domains


# --------------------------------------------------------------------------------------
# (c) owner_groups mirrors tier1_owner_groups
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_groups_match_tier1_owner_groups(db_session):
    """The histogram is the units' tier1_owner_groups summed, nothing added or dropped."""
    unit_ap = await _make_unit(db_session, [
        await _make_article(db_session, domain="apnews.com"),
        await _make_article(db_session, domain="live.apnews.com"),
    ])
    unit_bbc = await _make_unit(db_session, [
        await _make_article(db_session, domain="bbc.com"),
        await _make_article(db_session, domain="bbc.co.uk"),  # subdomain, still BBC
    ])
    story = await _make_story(db_session, [unit_ap, unit_bbc])
    await db_session.commit()

    await apply_tier1_gate(db_session, story_ids=[story.id])
    decision = (await _reload(db_session, story.id)).gate_decision

    expected = tier1_owner_histogram([unit_ap, unit_bbc])
    assert expected == {"AP": 2, "BBC": 2}
    assert decision["owner_groups"] == expected
    assert decision["owner_groups"] == {"AP": 2, "BBC": 2}
    # The invariant the payload is auditable by: it cannot disagree with the stored counter.
    assert decision["distinct_owners"] == len(decision["owner_groups"])
    assert decision["distinct_owners"] == decision["score_breakdown"]["distinct_owners"]
    assert decision["distinct_owners"] == story.distinct_owners == 2


@pytest.mark.asyncio
async def test_owner_groups_ignore_non_tier1_articles(db_session):
    """A tier-2 or tier-3 article is evidence, never an owner the gate counts."""
    unit = await _make_unit(db_session, [
        await _make_article(db_session, domain="bbc.com"),
        await _make_article(db_session, domain="theguardian.com"),
        await _make_article(db_session, domain="nytimes.com", tier=SourceTier.TIER2),
    ])
    story = await _make_story(db_session, [unit])
    await db_session.commit()

    await apply_tier1_gate(db_session, story_ids=[story.id])
    decision = (await _reload(db_session, story.id)).gate_decision

    assert decision["owner_groups"] == {"BBC": 1, "Guardian": 1}
    assert len(decision["contributing_articles"]) == 3


# --------------------------------------------------------------------------------------
# (d) wire services collapse to one owner
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wire_service_copies_are_not_independent_owners(db_session):
    """The README's claim, as a test: AP copy across many domains is still one owner.

    Six copies of one AP story, on four domains including a subdomain, clustered into two
    reporting units (the near-duplicate split a real run produces) and linked to one story.
    Two units is enough units to clear the threshold, so the only thing that can block this
    story is owner independence -- and there is none, so it must be blocked.
    """
    ap_copies = [
        await _make_article(db_session, domain=domain)
        for domain in ("apnews.com", "apnews.com", "live.apnews.com", "apnews.com")
    ]
    unit_one = await _make_unit(db_session, ap_copies[:2])
    unit_two = await _make_unit(db_session, ap_copies[2:])
    story = await _make_story(db_session, [unit_one, unit_two])
    await db_session.commit()

    result = await apply_tier1_gate(db_session, story_ids=[story.id])
    assert result == {"queued": 0, "blocked": 1}

    decision = (await _reload(db_session, story.id)).gate_decision
    assert decision["passed"] is False
    assert "only 1 owner" in decision["reason"]
    # Four articles, four domains, one owner. The count that would have inflated the gate is
    # the article count; the count that decides it is the owner count.
    assert decision["owner_groups"] == {"AP": 4}
    assert decision["distinct_owners"] == 1
    assert decision["tier1_unit_count"] == 4
    assert len(decision["contributing_articles"]) == 4
    assert {a["owner_group"] for a in decision["contributing_articles"]} == {"AP"}
    # One owner, but the copies still show which unit each arrived in.
    assert {a["unit_id"] for a in decision["contributing_articles"]} == {
        str(unit_one.id),
        str(unit_two.id),
    }


@pytest.mark.asyncio
async def test_two_wire_services_are_two_owners(db_session):
    """Collapsing is per wire, not global: AP plus Reuters still corroborate each other."""
    unit_ap = await _make_unit(db_session, [
        await _make_article(db_session, domain="apnews.com"),
        await _make_article(db_session, domain="live.apnews.com"),
    ])
    unit_reuters = await _make_unit(db_session, [
        await _make_article(db_session, domain="reuters.com"),
    ])
    story = await _make_story(db_session, [unit_ap, unit_reuters])
    await db_session.commit()

    result = await apply_tier1_gate(db_session, story_ids=[story.id])
    assert result == {"queued": 1, "blocked": 0}

    decision = (await _reload(db_session, story.id)).gate_decision
    assert decision["owner_groups"] == {"AP": 2, "Reuters": 1}
    assert decision["distinct_owners"] == 2


@pytest.mark.asyncio
async def test_wire_copy_from_an_unowned_domain_is_not_a_tier1_owner(db_session):
    """A copy on a domain OWNERSHIP_GROUPS does not know is 'Independent', and tier-gated out.

    This is the honest limit of the collapsing: attribution follows the domain, so a wire
    story republished by an unknown site resolves to "Independent" rather than to the wire.
    It still cannot reach the gate, because only tier-1 articles contribute owners, and the
    tier comes from the source registry rather than from the copy's content.
    """
    unit = await _make_unit(db_session, [
        await _make_article(db_session, domain="apnews.com"),
        await _make_article(db_session, domain="theguardian.com"),
        await _make_article(db_session, domain="local-paper.example", tier=SourceTier.TIER3),
    ])
    story = await _make_story(db_session, [unit])
    await db_session.commit()

    await apply_tier1_gate(db_session, story_ids=[story.id])
    decision = (await _reload(db_session, story.id)).gate_decision

    assert decision["owner_groups"] == {"AP": 1, "Guardian": 1}
    unknown = next(
        a for a in decision["contributing_articles"] if a["source_domain"] == "local-paper.example"
    )
    assert unknown["owner_group"] == "Independent"
    assert unknown["tier"] == "TIER3"


def test_build_gate_decision_is_deterministic_and_pins_its_timestamp():
    """The pure builder: sorted keys, deterministic article order, injectable clock."""
    stamp = datetime(2026, 10, 2, 12, 30, tzinfo=timezone.utc)
    articles = [
        {"url": "https://b.example/2", "source_domain": "b.example", "owner_group": "B", "tier": "TIER1", "unit_id": "u1"},
        {"url": "https://a.example/1", "source_domain": "a.example", "owner_group": "A", "tier": "TIER1", "unit_id": "u1"},
        {"url": "https://z.example/3", "source_domain": "z.example", "owner_group": "Z", "tier": "TIER3", "unit_id": "u2"},
    ]
    decision = build_gate_decision(
        passed=False,
        reason="Blocked: Tier-1 units from only 1 owner(s)",
        gate_mode="boolean",
        tier1_unit_count=2,
        owner_groups={"B": 1, "A": 1},
        articles=list(reversed(articles)),
        decided_at=stamp,
    )

    assert list(decision["owner_groups"]) == ["A", "B"]
    assert [a["url"] for a in decision["contributing_articles"]] == [
        "https://a.example/1",
        "https://b.example/2",
        "https://z.example/3",  # tier-3 last regardless of input order
    ]
    assert decision["decided_at"] == "2026-10-02T12:30:00+00:00"
    assert decision["admission"] is None
    assert decision["distinct_owners"] == 2