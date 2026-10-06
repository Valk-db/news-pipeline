"""Per-story failure isolation in apply_dynamic_gate (P0 deferred (a), 2026-10-02).

One story raising mid-gate used to end the whole run: the exception escaped the per-story loop
before its single `session.commit()`, so in enabled mode NO story was decided, and in shadow mode
the whole calibration pass (gate_shadow status rows + 'dynamic' decision rows) was lost. These
tests pin that one bad story now costs only itself: the run returns, the other stories are
decided and committed, and the failure is recorded on the bad story and in status_log.

Real SQLite through the shared `db_session` fixture, driving the real gate end to end. The
malformed `primary_entities` entry is the honest trigger: compute_harm_level() does
`uuid.UUID(eid)` on each entry, so a non-UUID string raises ValueError mid-gate.
"""

import uuid
from datetime import datetime, UTC

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from src.schema.models import (
    Claim,
    ClaimType,
    GateDecision,
    RawArticle,
    ReportingUnit,
    SourceTier,
    StatusLog,
    Story,
    StoryUnitLink,
)
from src.shared.config import get_settings
from src.verification.tiers import apply_dynamic_gate
from src.verification.units import get_owner_group

MALFORMED_ENTITY = "not-a-uuid"


def _utcnow():
    return datetime.now(UTC)


async def _make_article(session, *, domain):
    slug = uuid.uuid4().hex[:8]
    article = RawArticle(
        id=uuid.uuid4(),
        url=f"https://{domain}/story/{slug}",
        url_hash=uuid.uuid4().hex,
        title=f"Report from {domain} ({slug})",
        body_text=f"Body text for the {slug} report filed by {domain}.",
        source_domain=domain,
        source_tier=SourceTier.TIER1,
        published_at=_utcnow(),
        entities={"PERSON": ["Jane Doe"], "GPE": ["Lisbon"], "ORG": ["Ministry"]},
    )
    session.add(article)
    await session.flush()
    return article


async def _make_unit(session, articles):
    source_tiers, owner_groups, tier1_owner_groups = {}, {}, {}
    for article in articles:
        owner = get_owner_group(article.source_domain)
        source_tiers["tier1"] = source_tiers.get("tier1", 0) + 1
        owner_groups[owner] = owner_groups.get(owner, 0) + 1
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


async def _make_story(session, units, *, primary_entities):
    story = Story(
        id=uuid.uuid4(),
        day=_utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
        primary_entities=primary_entities,
        status=Story.Status.PENDING,
    )
    session.add(story)
    await session.flush()
    for unit in units:
        session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
    await session.flush()
    return story


async def _two_unit_story(session, domains):
    units = [
        await _make_unit(session, [await _make_article(session, domain=d)])
        for d in domains
    ]
    return await _make_story(session, units, primary_entities=[])


async def _plant(session):
    """One story that raises mid-gate, plus two that gate cleanly."""
    bad_units = [
        await _make_unit(session, [await _make_article(session, domain="bbc.com")]),
        await _make_unit(
            session, [await _make_article(session, domain="theguardian.com")]
        ),
    ]
    bad = await _make_story(session, bad_units, primary_entities=[MALFORMED_ENTITY])
    # compute_harm_level() only reaches the uuid.UUID() conversion when the story carries an
    # ALLEGATION claim, so the bad story needs one for the trigger to be real.
    session.add(
        Claim(
            id=uuid.uuid4(),
            story_id=bad.id,
            text="scratch allegation",
            claim_type=ClaimType.ALLEGATION,
            first_seen_at=_utcnow(),
        )
    )
    good1 = await _two_unit_story(session, ["bbc.com", "theguardian.com"])
    good2 = await _two_unit_story(session, ["npr.org", "cnn.com"])
    await session.commit()
    return bad, good1, good2


def _set_dynamic_gate(monkeypatch, enabled: bool) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "dynamic_gate_enabled", enabled)


async def _reload(session, story_id):
    return (await session.execute(select(Story).where(Story.id == story_id))).scalar_one()


async def _decisions(session, story_id):
    return list(
        (
            await session.execute(
                select(GateDecision)
                .where(GateDecision.story_id == story_id)
                .order_by(GateDecision.decided_at)
            )
        )
        .scalars()
        .all()
    )


async def _error_logs(session, story_id):
    return list(
        (
            await session.execute(
                select(StatusLog).where(
                    StatusLog.phase == "gate_error",
                    StatusLog.details.cast(sa.String).like(f"%{story_id}%"),
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_enabled_mode_one_bad_story_does_not_cost_the_run(db_session, monkeypatch):
    """Enabled: the good stories are decided, committed and counted; the bad one is not."""
    _set_dynamic_gate(monkeypatch, True)
    bad, good1, good2 = await _plant(db_session)

    result = await apply_dynamic_gate(db_session, [bad.id, good1.id, good2.id])

    assert result == {"queued": 2, "blocked": 0, "errors": 1}

    for good in (good1, good2):
        stored = await _reload(db_session, good.id)
        assert stored.status is Story.Status.PENDING
        assert "Dynamic gate passed" in stored.gate_reason
        (row,) = await _decisions(db_session, good.id)
        assert row.gate_name == "dynamic" and row.passed is True

    # The bad story is left exactly as the run found it: same status, an error instead of a
    # verdict, and no decision row pretending it was evaluated.
    bad_stored = await _reload(db_session, bad.id)
    assert bad_stored.status is Story.Status.PENDING, "an undecided story must not look decided"
    assert bad_stored.gate_reason.startswith("Gate error: not decided this run")
    assert "ValueError" in bad_stored.gate_reason
    assert await _decisions(db_session, bad.id) == []

    (log,) = await _error_logs(db_session, bad.id)
    assert log.status == "error"
    assert log.details["story_id"] == str(bad.id)
    assert log.details["error_type"] == "ValueError"
    assert log.details["shadow_only"] is False
    assert log.details["status_left_unchanged"] == Story.Status.PENDING.value


@pytest.mark.asyncio
async def test_shadow_mode_score_failure_keeps_the_tier1_decision(db_session, monkeypatch):
    """Shadow: the boolean gate's committed verdict survives; only the score is missing.

    In shadow mode the dynamic score is a measurement, not a verdict, so the failure must not
    overwrite the sentence the tier-1 gate already committed for the story.
    """
    _set_dynamic_gate(monkeypatch, False)
    bad, good1, good2 = await _plant(db_session)

    result = await apply_dynamic_gate(db_session, [bad.id, good1.id, good2.id])

    assert result["queued"] == 3 and result["errors"] == 1

    bad_stored = await _reload(db_session, bad.id)
    assert bad_stored.status is Story.Status.PENDING
    assert bad_stored.gate_reason.startswith("Passed gate:"), "tier-1 verdict preserved"
    assert "Dynamic score not computed" in bad_stored.gate_reason
    assert "ValueError" in bad_stored.gate_reason

    names = [row.gate_name for row in await _decisions(db_session, bad.id)]
    assert names == ["tier1"], "no shadow decision row can be recorded for a story that raised"

    for good in (good1, good2):
        rows = {row.gate_name: row for row in await _decisions(db_session, good.id)}
        assert set(rows) == {"tier1", "dynamic"}
        assert rows["dynamic"].breakdown["shadow"] is True

    (log,) = await _error_logs(db_session, bad.id)
    assert log.details["shadow_only"] is True


@pytest.mark.asyncio
async def test_enabled_mode_commits_the_other_stories_before_returning(db_session, monkeypatch):
    """The final commit still runs: a fresh read sees the good stories' decisions persisted.

    Before the fix the exception propagated before `await session.commit()`, so nothing this run
    decided reached the database. Re-reading in a second session is what proves the commit, not
    the in-memory identity map the first session already holds.
    """
    _set_dynamic_gate(monkeypatch, True)
    bad, good1, good2 = await _plant(db_session)

    await apply_dynamic_gate(db_session, [bad.id, good1.id, good2.id])

    async with db_session.bind.connect() as conn:
        persisted = await conn.execute(
            select(Story.gate_reason).where(Story.id == good1.id)
        )
        assert "Dynamic gate passed" in persisted.scalar_one()
        error_rows = await conn.execute(
            select(StatusLog.details).where(StatusLog.phase == "gate_error")
        )
        assert any(
            str(bad.id) in str(details) for (details,) in error_rows.all()
        ), "the gate_error row is committed too, not just pending in the session"
