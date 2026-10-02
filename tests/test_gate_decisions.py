"""Tests for the append-only `gate_decisions` ledger and the wire-copy collapse it explains.

The gate decides with data it already had -- the tier-1 owner histogram and the articles behind
it -- and before this table that data survived only as a sentence in `stories.gate_reason`. These
tests pin what an auditor or a query needs to be true of that record:

  a) every evaluation appends exactly one row per gate, with the score, the threshold and the
     breakdown the sentence in `gate_reason` reports;
  b) the row's evidence resolves the real chain (story_unit_links -> reporting_units ->
     raw_articles) and `len(owner_groups)` always equals the `distinct_owners` counter on the
     story row, so the record and the counter beside it cannot disagree;
  c) syndicated wire copies collapse onto the wire service that published the content -- the
     spec's fixture of 1 AP article plus 3 same-content_hash republications counts as ONE
     corroborating outlet, and says so in the reason and in `wire_origin` per article;
  d) the admission score counts the same independence the boolean gate does, so with the dynamic
     gate enabled the score cannot admit a story the boolean gate blocks, and its breakdown states
     the post-collapse owner count it was given;
  e) the table is append-only by construction: re-gating appends a second row and leaves the
     first readable, and no code path in this repo UPDATEs or DELETEs a row.

Real SQLite through the shared `db_session` fixture, not mocked sessions: the ledger's whole
purpose is what the database stores and what a query returns, and a mock proves neither. The
wire-collapse fixture additionally drives a real content_hash lookup, which is the mechanism.

(e) is asserted two ways on purpose. `test_score_agrees_with_the_boolean_gate_on_independence`
puts the same collapse fixture through both gates and requires the same verdict;
`test_score_agrees_with_the_boolean_gate_across_owner_counts` then holds that relation across
owner counts, so it is a property of the two gates rather than a coincidence of one example.

(d) is asserted two ways on purpose. `test_re_gating_appends_rather_than_overwrites` reads the
rows back. `test_no_sql_path_updates_or_deletes_a_decision` watches the actual SQL a re-gate
emits through a `before_cursor_execute` listener, so a future refactor that starts mutating rows
fails here even if the objects it returns still look right. The migration states the same
convention in prose; this is the part a comment cannot hold.
"""

import re
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, select

from src.schema.models import (
    GateDecision,
    RawArticle,
    ReportingUnit,
    SourceTier,
    Story,
    StoryUnitLink,
)
from src.shared.analyzer_versions import GATE_VERSION
from src.shared.config import get_settings
from src.verification.corroboration import WIRE_SERVICE_DOMAINS
from src.verification.tiers import apply_dynamic_gate, apply_tier1_gate
from src.verification.units import get_owner_group

ENTITIES = {"PERSON": ["Jane Doe"], "GPE": ["Lisbon"], "ORG": ["Ministry"]}

# One AP story as AP published it, and three other outlets carrying the identical bytes. The
# three must be three distinct ownership groups, or the pre-collapse count would not have been 4.
AP_CONTENT_HASH = "a" * 64
REPUBLISHERS = ["cnn.com", "washingtonpost.com", "npr.org"]


async def _make_article(
    session,
    *,
    domain: str,
    tier: SourceTier = SourceTier.TIER1,
    content_hash: str | None = None,
) -> RawArticle:
    slug = uuid.uuid4().hex[:8]
    article = RawArticle(
        id=uuid.uuid4(),
        url=f"https://{domain}/story/{slug}",
        url_hash=uuid.uuid4().hex,
        title=f"Report from {domain} ({slug})",
        body_text=f"Body text for the {slug} report filed by {domain}.",
        source_domain=domain,
        source_tier=tier,
        published_at=datetime.now(timezone.utc),
        entities=ENTITIES,
        content_hash=content_hash,
    )
    session.add(article)
    await session.flush()
    return article


async def _make_unit(session, articles: list[RawArticle]) -> ReportingUnit:
    """Cluster `articles` into one reporting unit the way build_reporting_units() does.

    The owner arithmetic is copied from src/verification/units.py on purpose: a fixture that
    hard-coded {"AP": 1} would keep passing if the real owner resolution changed. Note what it
    deliberately does NOT do -- collapse wire copies. That is the gate's job now, so the stored
    histogram stays the pre-collapse count the pipeline has always written.
    """
    source_tiers: dict[str, int] = {}
    owner_groups: dict[str, int] = {}
    tier1_owner_groups: dict[str, int] = {}
    for article in articles:
        owner = get_owner_group(article.source_domain)
        tier = article.source_tier.value
        source_tiers[tier] = source_tiers.get(tier, 0) + 1
        owner_groups[owner] = owner_groups.get(owner, 0) + 1
        if article.source_tier is SourceTier.TIER1:
            tier1_owner_groups[owner] = tier1_owner_groups.get(owner, 0) + 1

    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
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


async def _make_story(session, units: list[ReportingUnit]) -> Story:
    story = Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        primary_entities=[],
        status=Story.Status.PENDING,
    )
    session.add(story)
    await session.flush()
    for unit in units:
        session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
    await session.flush()
    return story


async def _one_unit_per_article(
    session, domains: list[str], content_hash: str | None = None
) -> Story:
    """A story whose units are one per outlet, which is how syndication actually lands."""
    units = []
    for domain in domains:
        article = await _make_article(session, domain=domain, content_hash=content_hash)
        units.append(await _make_unit(session, [article]))
    return await _make_story(session, units)


async def _wire_collapse_story(session) -> Story:
    """The spec's collapse fixture: 1 AP article + 3 same-content_hash republications.

    Four tier-1 articles from four pre-collapse owner groups, one independent outlet post-collapse.
    Shared by the boolean-gate test and the score test, because the whole point is that both gates
    are handed the same evidence and must reach the same verdict from it.
    """
    ap_article = await _make_article(session, domain="apnews.com", content_hash=AP_CONTENT_HASH)
    units = [await _make_unit(session, [ap_article])]
    units += [
        await _make_unit(
            session,
            [await _make_article(session, domain=domain, content_hash=AP_CONTENT_HASH)],
        )
        for domain in REPUBLISHERS
    ]
    return await _make_story(session, units)


async def _decisions(session, story_id) -> list[GateDecision]:
    """Every row for a story, oldest first -- the order a reader of the ledger would see."""
    result = await session.execute(
        select(GateDecision)
        .where(GateDecision.story_id == story_id)
        .order_by(GateDecision.decided_at)
    )
    return list(result.scalars().all())


def _by_gate(rows: list[GateDecision]) -> dict[str, GateDecision]:
    return {row.gate_name: row for row in rows}


async def _reload(session, story_id) -> Story:
    result = await session.execute(select(Story).where(Story.id == story_id))
    return result.scalar_one()


# =====================================================================================
# (a) one row per evaluation
# =====================================================================================


@pytest.mark.asyncio
async def test_tier1_gate_records_one_row_per_story(db_session):
    """A passing story, a blocked story, one row each, and each row agrees with its story."""
    corroborated = await _one_unit_per_article(db_session, ["bbc.com", "theguardian.com"])
    lonely = await _one_unit_per_article(db_session, ["bbc.com"])
    await db_session.commit()

    result = await apply_tier1_gate(db_session, story_ids=[corroborated.id, lonely.id])
    assert result == {"queued": 1, "blocked": 1}

    passed_row = (await _decisions(db_session, corroborated.id))[0]
    blocked_row = (await _decisions(db_session, lonely.id))[0]
    assert passed_row.passed is True
    assert blocked_row.passed is False

    # The boolean gate has no score; recording one would invent arithmetic it did not do.
    assert passed_row.score is None
    assert passed_row.pass_threshold is None
    assert passed_row.breakdown is None

    for story, row, expected in (
        (corroborated, passed_row, True),
        (lonely, blocked_row, False),
    ):
        stored = await _reload(db_session, story.id)
        assert row.gate_name == "tier1"
        assert row.gate_version == GATE_VERSION
        assert row.analyzer_version == GATE_VERSION
        assert row.passed is (stored.status is Story.Status.PENDING) is expected
        assert row.tier1_unit_count == stored.tier1_unit_count
        # One count, two columns: the row cannot disagree with the counter printed beside it.
        assert row.distinct_owners == stored.distinct_owners == len(row.owner_groups)
        # The stored histogram counts tier-1 articles per owner, post-collapse.
        assert sum(row.owner_groups.values()) == row.tier1_unit_count
        assert stored.gate_reason, "gate_reason is still written for scripts/ and the curation UI"


@pytest.mark.asyncio
async def test_dynamic_gate_records_the_score_it_decided_on(db_session):
    """With the flag on, the dynamic row is the decision, and its arithmetic is stored."""
    settings = get_settings()
    original = settings.dynamic_gate_enabled
    settings.dynamic_gate_enabled = True
    try:
        story = await _one_unit_per_article(db_session, ["bbc.com", "npr.org"])
        await db_session.commit()

        result = await apply_dynamic_gate(db_session, [story.id])
        assert result == {"queued": 1, "blocked": 0}

        (row,) = await _decisions(db_session, story.id)
        assert row.gate_name == "dynamic"
        assert row.passed is True
        assert row.score == row.breakdown["final_score"]
        assert row.pass_threshold == row.breakdown["pass_threshold"]
        assert "shadow" not in row.breakdown, "an applied score is not a shadow score"

        # Every number the sentence reports is the number the row stores, so a reader of
        # gate_reason and a query on breakdown cannot be told different stories.
        stored = await _reload(db_session, story.id)
        for key, value in row.breakdown.items():
            if key in ("passes", "pass_threshold"):
                continue
            assert f"{key}={value}" in stored.gate_reason
        assert f"score={row.score}" in stored.gate_reason
    finally:
        settings.dynamic_gate_enabled = original


@pytest.mark.asyncio
async def test_shadow_mode_records_both_the_decision_and_the_score(db_session):
    """Shadow mode: the boolean gate decides, and the score it would have used is still a row."""
    settings = get_settings()
    original = settings.dynamic_gate_enabled
    settings.dynamic_gate_enabled = False
    try:
        story = await _one_unit_per_article(db_session, ["bbc.com", "theguardian.com"])
        await db_session.commit()

        result = await apply_dynamic_gate(db_session, [story.id])
        assert result == {"queued": 1, "blocked": 0}

        rows = _by_gate(await _decisions(db_session, story.id))
        assert set(rows) == {"tier1", "dynamic"}
        boolean, shadow = rows["tier1"], rows["dynamic"]

        assert boolean.passed is True and boolean.score is None
        assert shadow.breakdown["shadow"] is True, "an unapplied score must say so"
        assert shadow.breakdown["final_score"] == shadow.score
        # The calibration question this row exists to answer: did the score agree with the gate
        # that actually decided?
        assert shadow.passed is (shadow.score >= shadow.pass_threshold)
        assert boolean.passed == shadow.passed
        stored = await _reload(db_session, story.id)
        assert stored.status is Story.Status.PENDING
    finally:
        settings.dynamic_gate_enabled = original


# =====================================================================================
# (b) the evidence is the real chain, and agrees with the counter
# =====================================================================================


@pytest.mark.asyncio
async def test_contributing_articles_resolve_the_real_chain(db_session):
    """The recorded articles are the RawArticle rows behind the units, with every key."""
    story = await _one_unit_per_article(db_session, ["bbc.com", "theguardian.com"])
    await db_session.commit()

    await apply_tier1_gate(db_session, story_ids=[story.id])

    (row,) = await _decisions(db_session, story.id)
    by_domain = {a["source_domain"]: a for a in row.contributing_articles}
    assert set(by_domain) == {"bbc.com", "theguardian.com"}
    for article in by_domain.values():
        assert set(article) == {
            "article_id",
            "url",
            "source_domain",
            "tier",
            "wire_origin",
            "content_hash",
        }
        assert article["tier"] == "tier1"
        assert article["url"].startswith("https://")

    # The ids are the actual rows, not a reconstruction of them.
    stored_rows = await db_session.execute(
        select(RawArticle.id, RawArticle.url).where(
            RawArticle.reporting_unit_id.is_not(None)
        )
    )
    assert {a["article_id"]: a["url"] for a in row.contributing_articles} == {
        str(r[0]): r[1] for r in stored_rows.all()
    }


# =====================================================================================
# (c) the wire collapse
# =====================================================================================


@pytest.mark.asyncio
async def test_wire_copies_collapse_to_one_owner(db_session):
    """The spec's fixture: 1 AP article + 3 same-content_hash republications is ONE outlet.

    Before the collapse this story had four distinct tier-1 owners and passed the gate. After it
    it has one, is blocked, and says so in both the row and the reason. This is the only intended
    change in behavior, and it is a narrowing: a story can only lose corroboration here.
    """
    assert "apnews.com" in WIRE_SERVICE_DOMAINS, "the fixture depends on the registry"

    # The pre-collapse world: four outlets, four distinct owners, the gate passes.
    pre_collapse = {get_owner_group(domain) for domain in ["apnews.com", *REPUBLISHERS]}
    assert len(pre_collapse) == 4, f"fixture must be 4 owners pre-collapse, got {pre_collapse}"

    story = await _wire_collapse_story(db_session)
    await db_session.commit()

    result = await apply_tier1_gate(db_session, story_ids=[story.id])
    assert result == {"queued": 0, "blocked": 1}, "four outlets carrying one story is one outlet"

    (row,) = await _decisions(db_session, story.id)
    assert row.distinct_owners == 1
    assert row.distinct_owners == len(row.owner_groups) == 1
    assert row.owner_groups == {"AP": 4}, "all four articles attribute to the AP group"
    assert row.tier1_unit_count == 4, "collapsing changes owner count, not article count"

    # The three republications carry the wire origin; AP's own article is the origin, not a copy.
    copies = {a["source_domain"]: a for a in row.contributing_articles if a["wire_origin"]}
    assert set(copies) == set(REPUBLISHERS)
    for article in copies.values():
        assert article["wire_origin"] == "apnews.com"
        assert article["content_hash"] == AP_CONTENT_HASH
    for domain in REPUBLISHERS:
        assert get_owner_group(domain) != "AP", f"{domain} is its own owner pre-collapse"

    origin = next(
        a for a in row.contributing_articles if a["source_domain"] == "apnews.com"
    )
    assert origin["wire_origin"] is None, "the wire's own article is not a copy of itself"

    stored = await _reload(db_session, story.id)
    assert "3 wire copies collapsed to AP" in stored.gate_reason
    assert stored.status is Story.Status.BLOCKED


@pytest.mark.asyncio
async def test_collapsing_is_reported_per_wire_service(db_session):
    """Two wires in one story: the note names each, so the count is checkable."""
    ap = await _make_article(db_session, domain="apnews.com", content_hash="a" * 64)
    reuters = await _make_article(db_session, domain="reuters.com", content_hash="b" * 64)
    cnn_copy = await _make_article(db_session, domain="cnn.com", content_hash="a" * 64)
    npr_copy = await _make_article(db_session, domain="npr.org", content_hash="b" * 64)
    story = await _make_story(
        db_session,
        [await _make_unit(db_session, [a]) for a in (ap, reuters, cnn_copy, npr_copy)],
    )
    await db_session.commit()

    await apply_tier1_gate(db_session, story_ids=[story.id])

    (row,) = await _decisions(db_session, story.id)
    assert row.owner_groups == {"AP": 2, "Reuters": 2}
    stored = await _reload(db_session, story.id)
    assert "1 wire copy collapsed to AP" in stored.gate_reason
    assert "1 wire copy collapsed to Reuters" in stored.gate_reason


@pytest.mark.asyncio
async def test_identical_content_without_a_wire_origin_does_not_collapse(db_session):
    """The documented limitation: no wire row with that content_hash means no collapse.

    The registry's wire sources are disabled today (apnews.com, reuters.com), so in live data
    there is usually nothing to collapse onto and identical articles count as themselves. This
    test states that behavior so a future change to it is a deliberate, visible one.
    """
    shared = "c" * 64
    story = await _one_unit_per_article(
        db_session, ["bbc.com", "theguardian.com", "cnn.com"], content_hash=shared
    )
    await db_session.commit()

    result = await apply_tier1_gate(db_session, story_ids=[story.id])
    assert result == {"queued": 1, "blocked": 0}

    (row,) = await _decisions(db_session, story.id)
    assert row.distinct_owners == 3
    assert all(a["wire_origin"] is None for a in row.contributing_articles)
    stored = await _reload(db_session, story.id)
    assert "wire cop" not in stored.gate_reason


# =====================================================================================
# (e) the score and the boolean gate measure the same independence
# =====================================================================================


@pytest.mark.asyncio
async def test_score_agrees_with_the_boolean_gate_on_independence(db_session):
    """The collapse fixture scores below threshold, so the dynamic gate blocks it too.

    Before this, the same story scored 75 and passed while the boolean gate blocked it: the score's
    factors read the raw tier-1 article count, so four copies of one wire story counted as four
    corroborating voices. With the dynamic gate enabled the score *is* the admission decision, so
    that split admitted exactly what the boolean gate holds out. Both gates now count the
    post-collapse owner histogram, and the breakdown records which count they were given.
    """
    settings = get_settings()
    original = settings.dynamic_gate_enabled
    settings.dynamic_gate_enabled = True
    try:
        story = await _wire_collapse_story(db_session)
        await db_session.commit()

        # The boolean gate, on the same evidence: one independent outlet, blocked.
        assert await apply_tier1_gate(db_session, story_ids=[story.id]) == {
            "queued": 0,
            "blocked": 1,
        }

        result = await apply_dynamic_gate(db_session, [story.id])
        assert result == {"queued": 0, "blocked": 1}, "the score must not admit what the gate held"

        (score_row,) = [
            row for row in await _decisions(db_session, story.id) if row.gate_name == "dynamic"
        ]
        breakdown = score_row.breakdown
        # One owner, four articles: the boolean gate's full admission condition (>= 2 tier-1
        # units AND >= 2 distinct owners) fails on owners, so the independence factors award
        # nothing -- a single outlet cannot corroborate itself, and the score cannot admit what
        # the gate held out. Nothing else on this fixture (no tier3/4 units to be viral, no
        # ALLEGATION claim).
        assert breakdown["independent_owners"] == 1
        assert breakdown["tier1_units"] == 4
        assert breakdown["tier_baseline"] == 0
        assert breakdown["corroboration"] == 0
        assert breakdown["base_score"] == score_row.score == 0
        assert score_row.score < score_row.pass_threshold == 50
        assert score_row.passed is False
        assert breakdown["passes"] is False

        # The row reports four tier-1 articles but counted one owner, and the breakdown says so:
        # the raw count is reporting, the owner count is the decision.
        assert score_row.tier1_unit_count == 4
        assert score_row.distinct_owners == 1
        assert "independent_owners=1" in (await _reload(db_session, story.id)).gate_reason
    finally:
        settings.dynamic_gate_enabled = original


@pytest.mark.asyncio
async def test_independently_corroborated_story_still_scores_above_threshold(db_session):
    """The change is a narrowing of the collapse case only: a real 2-owner story still passes."""
    settings = get_settings()
    original = settings.dynamic_gate_enabled
    settings.dynamic_gate_enabled = True
    try:
        story = await _one_unit_per_article(db_session, ["bbc.com", "theguardian.com", "npr.org"])
        await db_session.commit()

        result = await apply_dynamic_gate(db_session, [story.id])
        assert result == {"queued": 1, "blocked": 0}

        (row,) = await _decisions(db_session, story.id)
        assert row.gate_name == "dynamic"
        assert row.passed is True
        assert row.distinct_owners == 3
        assert row.breakdown["independent_owners"] == 3
        # 40 baseline + 30 corroboration (capped) + 15 owners.
        assert row.breakdown["base_score"] == row.score == 85
        assert row.score >= row.pass_threshold
    finally:
        settings.dynamic_gate_enabled = original


@pytest.mark.asyncio
async def test_score_agrees_with_the_boolean_gate_across_owner_counts(db_session):
    """One table of the two gates over the same evidence, for every owner count the gate sees.

    Pinned as a relation rather than as scattered examples: the score must be `>= threshold` exactly
    when the boolean gate passes, so neither gate can admit a story the other holds out. The one
    documented exception is harm_level == 'high', which raises the score's bar to 70 by design
    (GRAND_PLAN §3) and is not exercised by these fixtures.
    """
    settings = get_settings()
    original = settings.dynamic_gate_enabled
    settings.dynamic_gate_enabled = True
    try:
        for domains, expect_queued in (
            (["apnews.com"], False),                                  # 1 owner, 1 unit
            (["bbc.com", "bbc.com", "bbc.com"], False),              # 1 owner, 3 units
            (["bbc.com", "theguardian.com"], True),                  # 2 owners, 2 units
            (["bbc.com", "theguardian.com", "npr.org"], True),        # 3 owners, 3 units
        ):
            story = await _one_unit_per_article(db_session, domains)
            await db_session.commit()

            boolean = await apply_tier1_gate(db_session, story_ids=[story.id])
            dynamic = await apply_dynamic_gate(db_session, [story.id])

            (row,) = [
                r for r in await _decisions(db_session, story.id) if r.gate_name == "dynamic"
            ]
            owners = row.distinct_owners
            assert owners == len(set(get_owner_group(d) for d in domains))
            assert boolean == dynamic == (
                {"queued": 1, "blocked": 0} if expect_queued else {"queued": 0, "blocked": 1}
            ), f"{len(domains)} unit(s), {owners} owner(s)"
            assert row.passed is (row.score >= row.pass_threshold)
            assert row.passed is expect_queued
    finally:
        settings.dynamic_gate_enabled = original


# =====================================================================================
# (d) append-only
# =====================================================================================


@pytest.mark.asyncio
async def test_re_gating_appends_rather_than_overwrites(db_session):
    """Re-gating a story adds a row; the first decision stays readable and unchanged."""
    story = await _one_unit_per_article(db_session, ["bbc.com", "theguardian.com"])
    await db_session.commit()

    await apply_tier1_gate(db_session, story_ids=[story.id])
    first = (await _decisions(db_session, story.id))[0]
    first_id, first_hash, first_at = first.id, first.input_hash, first.decided_at

    await apply_tier1_gate(db_session, story_ids=[story.id])
    rows = await _decisions(db_session, story.id)
    assert len(rows) == 2, "a second decision is a second row"
    assert rows[0].id == first_id
    assert rows[0].input_hash == first_hash, "the first row was rewritten"
    assert rows[0].decided_at == first_at
    # Same evidence, same gate, so the recomputable digest matches: a no-op re-gate shows up as
    # a duplicate rather than as a change.
    assert rows[1].input_hash == first_hash
    assert rows[1].decided_at >= first_at


@pytest.mark.asyncio
async def test_no_sql_path_updates_or_deletes_a_decision(db_session, db_engine):
    """No code path mutates a row: watch the SQL, not the objects it hands back.

    `gate_decisions` is append-only by convention (see the migration). An ORM row read after an
    UPDATE looks identical to the one before it, so this asserts on the statements themselves:
    two gate runs over one story must emit two INSERTs into gate_decisions and nothing else.
    """
    statements: list[str] = []

    def record(conn, cursor, statement, *rest):
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    try:
        story = await _one_unit_per_article(db_session, ["bbc.com", "theguardian.com"])
        await db_session.commit()

        await apply_tier1_gate(db_session, story_ids=[story.id])
        await apply_tier1_gate(db_session, story_ids=[story.id])
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)

    mutating = [
        s for s in statements
        if re.search(r"(?i)\b(update|delete)\b[^\n]*\bgate_decisions\b", s)
    ]
    assert mutating == [], f"gate_decisions is append-only, never mutated: {mutating}"
    inserts = [
        s for s in statements
        if re.search(r"(?i)\binsert\b[^\n]*\bgate_decisions\b", s)
    ]
    assert len(inserts) == 2, "one INSERT per evaluated story per gate run"


@pytest.mark.asyncio
async def test_input_hash_is_a_digest_of_the_evidence(db_session):
    """input_hash is a SHA256 hex digest, and it moves when the evidence moves."""
    story = await _one_unit_per_article(db_session, ["bbc.com", "theguardian.com"])
    await db_session.commit()
    await apply_tier1_gate(db_session, story_ids=[story.id])
    (first,) = await _decisions(db_session, story.id)
    assert re.fullmatch(r"[0-9a-f]{64}", first.input_hash)

    # A third outlet joins the same story: the same gate now sees more evidence.
    third = await _make_unit(
        db_session, [await _make_article(db_session, domain="npr.org")]
    )
    db_session.add(StoryUnitLink(story_id=story.id, unit_id=third.id))
    await db_session.commit()
    await apply_tier1_gate(db_session, story_ids=[story.id])

    rows = await _decisions(db_session, story.id)
    assert rows[1].input_hash != first.input_hash
    assert rows[1].distinct_owners == 3
    assert rows[1].tier1_unit_count == 3
