"""Unpublish takes a story off the public surfaces, and says so.

Two things are being asserted here and they are not the same thing:

  1. `scripts/unpublish_story.py` writes the row and the audit entry.
  2. The row it writes actually stops a public route serving the story.

(2) is the deliverable. The script alone would be satisfied by a test that only
ever looked at the database; what matters to an operator is that `/stories/{id}`
404s afterwards. Every end-to-end test here therefore asserts the route served the
story BEFORE the unpublish, in the same test -- a 404 with no positive control is
indistinguishable from a fixture the public surfaces never served at all.

The route filters are read only in this file, on purpose: PUBLIC_STORY_STATUSES
already excludes REJECTED and that is the property under test. This batch changes
no route and no filter.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from scripts.unpublish_story import (
    EXIT_NO_DATABASE,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    StoryNotFound,
    build_parser,
    main,
    run,
    unpublish_story,
)
from src.schema.models import (
    Event,
    RawArticle,
    ReportingUnit,
    SourceTier,
    StatusLog,
    Story,
    StoryUnitLink,
)
from src.shared import database as database_module
from src.shared.config import Settings, get_settings


@pytest.fixture
def test_settings(monkeypatch):
    """Settings with a database, set in a fixture so the session is not poisoned.

    monkeypatch rather than os.environ.setdefault at module scope: the module-scope
    form is permanent for the whole pytest session and has silently killed DB tests
    in other files before.
    """
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def wired_db(test_settings, db_engine):
    """The script's own engine path pointed at the in-memory test database.

    scripts/unpublish_story.py goes through src.shared.database._get_engine, which
    is the same seam the app uses, so injecting here exercises the real lookup
    instead of a hand-made session.
    """
    database_module._engine = db_engine
    database_module._async_session_maker = None
    return db_engine


@pytest.fixture
def app_with_db(wired_db):
    """The public app wired to the same database."""
    import curation_ui.main as main_module
    from curation_ui.main import app

    main_module.settings = get_settings()

    import src.shared.llm as llm_module

    llm_module._llm_client = None
    return app


def _make_public_story(db_session, *, status=Story.Status.QUEUED):
    """A story shaped so every public surface actually serves it.

    QUEUED because PUBLIC_STORY_STATUSES is (QUEUED, POSTED). Two tier-1 units and
    two owners because /api/map/stories defaults to min_owners=2 with
    min_tier1_sources=2, and an Event because the top-stories query inner-joins the
    event aggregate -- a story with no events cannot appear there at any status.
    Get any of these wrong and the "after" assertion silently degrades to "absent
    because it was never present".
    """
    now = datetime.now(timezone.utc)
    story = Story(
        id=uuid.uuid4(),
        day=now - timedelta(hours=2),
        primary_entities=["test-entity"],
        status=status,
        tier1_unit_count=2,
        tier2_unit_count=0,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=2,
        gate_reason="Passed gate: 2 tier-1 units, 2 distinct owners",
    )
    db_session.add(story)

    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=now - timedelta(hours=2),
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"example.com": 1},
        tier1_owner_groups={"example.com": 1},
    )
    article = RawArticle(
        id=unit.representative_article_id,
        url=f"https://example.com/{unit.id}",
        url_hash=str(unit.id).replace("-", ""),
        title="Report from Kyiv",
        source_domain="example.com",
        source_tier=SourceTier.TIER1,
        reporting_unit_id=unit.id,
    )
    db_session.add(unit)
    db_session.add(article)
    db_session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
    db_session.add(Event(
        id=uuid.uuid4(),
        story_id=story.id,
        latitude=50.45,
        longitude=30.52,
        location_name="Kyiv",
        location_type="city",
        radius_km=25.0,
        start_time=now - timedelta(hours=2),
        event_type=Event.EventType.CONFLICT,
        confidence=0.8,
        source_count=2,
        tier1_source_count=2,
        entities={"GPE": ["Kyiv"]},
    ))
    return story


async def _status_on_row(db_session, story_id) -> Story.Status:
    """The status read back off the row in the database.

    A column select rather than session.get(Story, ...): the fixtures are built with
    expire_on_commit=False, so get() answers from the identity map and would report
    the in-memory value of an object the code under test never touched. That made
    one test pass while the commit it was checking had not happened.
    """
    result = await db_session.execute(select(Story.status).where(Story.id == story_id))
    return result.scalar()


async def _unpublish_logs(db_session):
    result = await db_session.execute(
        select(StatusLog).where(StatusLog.phase == "unpublish").order_by(StatusLog.id)
    )
    return list(result.scalars())


class TestStatusWrite:
    """The row, read back from the database after the call."""

    @pytest.mark.asyncio
    async def test_sets_the_row_to_rejected(self, db_session):
        story = _make_public_story(db_session)
        await db_session.commit()

        result = await unpublish_story(
            db_session, story.id, reason="unverified allegation", actor="procmon"
        )

        assert result.changed is True
        assert result.wrote is True
        assert result.previous_status == Story.Status.QUEUED
        assert await _status_on_row(db_session, story.id) == Story.Status.REJECTED

    @pytest.mark.asyncio
    async def test_write_is_committed_not_merely_flushed(self, db_session, db_engine):
        """A different session must see REJECTED: log_status() commits the UPDATE too."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        story = _make_public_story(db_session)
        await db_session.commit()

        await unpublish_story(db_session, story.id, reason="take down", actor="procmon")

        other = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        async with other() as session:
            fresh = await session.get(Story, story.id)
            assert fresh.status == Story.Status.REJECTED

    @pytest.mark.asyncio
    async def test_unknown_id_raises_and_writes_nothing(self, db_session):
        missing = uuid.uuid4()
        _make_public_story(db_session)
        await db_session.commit()

        with pytest.raises(StoryNotFound):
            await unpublish_story(db_session, missing, reason="x", actor="procmon")

        assert await _unpublish_logs(db_session) == []


class TestAuditLog:
    """The status_log row, which is the whole reason --reason is mandatory."""

    @pytest.mark.asyncio
    async def test_log_entry_answers_who_when_and_why(self, db_session):
        story = _make_public_story(db_session)
        await db_session.commit()

        await unpublish_story(
            db_session,
            story.id,
            reason="named party, single unverified source",
            actor="procmon",
        )

        rows = await _unpublish_logs(db_session)
        assert len(rows) == 1
        entry = rows[0]
        # when -- run_at is the column StatusLog carries for every other action.
        assert entry.run_at is not None
        # who
        assert entry.details["actor"] == "procmon"
        # why
        assert entry.details["reason"] == "named party, single unverified source"
        # what changed, and by which tool
        assert entry.details["story_id"] == str(story.id)
        assert entry.details["previous_status"] == Story.Status.QUEUED.value
        assert entry.details["new_status"] == Story.Status.REJECTED.value
        assert entry.details["tool"] == "scripts/unpublish_story.py"
        # status_log.status is "ok, warn, error"; anything else reads as a failed retraction
        assert entry.status == "ok"

    @pytest.mark.asyncio
    async def test_log_row_and_status_land_together(self, db_session):
        """No log row without the retraction, no retraction without the log row.

        log_status() commits, so the UPDATE and the INSERT flush in one
        transaction. Splitting them across sessions would let either exist alone.
        """
        story = _make_public_story(db_session)
        await db_session.commit()

        await unpublish_story(db_session, story.id, reason="both or neither", actor="t")

        assert await _status_on_row(db_session, story.id) == Story.Status.REJECTED
        assert len(await _unpublish_logs(db_session)) == 1


class TestIdempotence:
    """Re-running is a stated no-op: not an error, and not a second log row."""

    @pytest.mark.asyncio
    async def test_second_run_writes_nothing(self, db_session):
        story = _make_public_story(db_session)
        await db_session.commit()

        first = await unpublish_story(db_session, story.id, reason="one", actor="t")
        second = await unpublish_story(db_session, story.id, reason="two", actor="t")

        assert first.changed is True
        assert second.changed is False
        assert second.wrote is False
        assert second.already_rejected is True
        assert await _status_on_row(db_session, story.id) == Story.Status.REJECTED
        # Exactly one audit row, carrying the FIRST reason. A silent double-write
        # would make the ledger claim two retractions happened.
        rows = await _unpublish_logs(db_session)
        assert len(rows) == 1
        assert rows[0].details["reason"] == "one"

    @pytest.mark.asyncio
    async def test_run_exits_zero_and_says_no_op_for_an_already_rejected_story(
        self, wired_db, db_session, capsys
    ):
        story = _make_public_story(db_session, status=Story.Status.REJECTED)
        await db_session.commit()

        code = await run(story.id, reason="redundant", actor="t")

        assert code == EXIT_OK
        assert "no-op" in capsys.readouterr().out
        assert await _unpublish_logs(db_session) == []

    @pytest.mark.asyncio
    async def test_run_exits_zero_and_reports_the_change(self, wired_db, db_session, capsys):
        story = _make_public_story(db_session)
        await db_session.commit()

        code = await run(story.id, reason="misattributed", actor="procmon")

        assert code == EXIT_OK
        out = capsys.readouterr().out
        assert str(story.id) in out
        assert "misattributed" in out
        assert await _status_on_row(db_session, story.id) == Story.Status.REJECTED


class TestDryRun:
    """A preview that writes nothing is useful; a preview that writes is a bug."""

    @pytest.mark.asyncio
    async def test_dry_run_reports_the_change_and_leaves_the_row_alone(
        self, wired_db, db_session
    ):
        story = _make_public_story(db_session)
        await db_session.commit()

        code = await run(story.id, reason="preview", actor="t", dry_run=True)

        assert code == EXIT_OK
        assert await _status_on_row(db_session, story.id) == Story.Status.QUEUED
        assert await _unpublish_logs(db_session) == []

    def test_dry_run_needs_no_reason(self, monkeypatch):
        """--dry-run without --reason is a legal preview; only a real write needs one."""
        # Pin "no database" explicitly rather than inheriting whatever the process
        # happens to have: a stray DATABASE_URL here would make this test open a
        # real connection to prove something about argument order.
        monkeypatch.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()

        with pytest.raises(SystemExit) as excinfo:
            main(["not-a-uuid", "--dry-run"])
        assert excinfo.value.code == EXIT_USAGE

        # With a valid id the parser gets past argument validation and the run stops
        # at the missing database instead, which is the correct refusal order:
        # the tool never demands a reason for a preview it will not act on.
        assert main([str(uuid.uuid4()), "--dry-run"]) == EXIT_NO_DATABASE


class TestRefusals:
    """A tool that exits 0 having unpublished nothing is the dangerous failure mode."""

    @pytest.mark.asyncio
    async def test_missing_story_exits_one_and_names_the_id(self, wired_db, db_session, capsys):
        missing = uuid.uuid4()
        _make_public_story(db_session)
        await db_session.commit()

        code = await run(missing, reason="typo?", actor="t")

        assert code == EXIT_NOT_FOUND
        out = capsys.readouterr().out
        assert str(missing) in out
        assert "Nothing was written" in out

    @pytest.mark.asyncio
    async def test_no_database_exits_three(self, wired_db, db_session, monkeypatch, capsys):
        import scripts.unpublish_story as script

        monkeypatch.setattr(script, "get_settings", lambda: Settings(database_url=""))
        story = _make_public_story(db_session)
        await db_session.commit()

        code = await run(story.id, reason="no db", actor="t")

        assert code == EXIT_NO_DATABASE
        assert "DATABASE_URL" in capsys.readouterr().out
        assert await _status_on_row(db_session, story.id) == Story.Status.QUEUED

    def test_missing_id_is_a_usage_error(self):
        with pytest.raises(SystemExit) as excinfo:
            build_parser().parse_args([])
        assert excinfo.value.code == EXIT_USAGE

    def test_integer_id_is_refused_because_ids_are_uuids(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            main(["123"])
        assert excinfo.value.code == EXIT_USAGE

    def test_garbage_id_is_refused(self):
        with pytest.raises(SystemExit) as excinfo:
            main(["../../etc/passwd"])
        assert excinfo.value.code == EXIT_USAGE

    def test_reason_is_required_for_a_real_write(self):
        with pytest.raises(SystemExit) as excinfo:
            main([str(uuid.uuid4())])
        assert excinfo.value.code == EXIT_USAGE

    def test_blank_reason_is_refused_too(self):
        with pytest.raises(SystemExit) as excinfo:
            main([str(uuid.uuid4()), "--reason", "   "])
        assert excinfo.value.code == EXIT_USAGE

    def test_help_names_the_exit_codes_and_the_flags(self):
        help_text = build_parser().format_help()
        assert "--dry-run" in help_text
        assert "--reason" in help_text
        assert "1 no such story" in help_text


class TestPublicSurfaceStopsServingIt:
    """The deliverable: drive the app before and after, inside one test."""

    @pytest.mark.asyncio
    async def test_story_page_serves_then_404s(self, app_with_db, db_session):
        story = _make_public_story(db_session)
        await db_session.commit()
        client = TestClient(app_with_db)

        # Positive control. Without it the 404 below proves nothing: an id that never
        # existed 404s too, and so does a fixture no public surface ever served.
        before = client.get(f"/stories/{story.id}")
        assert before.status_code == 200, before.text[:400]
        assert "Report from Kyiv" in before.text

        await unpublish_story(
            db_session, story.id, reason="retraction: wrong grouping", actor="procmon"
        )

        after = client.get(f"/stories/{story.id}")
        assert after.status_code == 404

    @pytest.mark.asyncio
    async def test_map_stories_list_drops_it(self, app_with_db, db_session):
        story = _make_public_story(db_session)
        await db_session.commit()
        client = TestClient(app_with_db)

        before = client.get("/api/map/stories")
        assert before.status_code == 200
        listed_before = [s["story_id"] for s in before.json()["stories"]]
        assert str(story.id) in listed_before

        await unpublish_story(db_session, story.id, reason="off the map", actor="procmon")

        after = client.get("/api/map/stories")
        assert after.status_code == 200
        listed_after = [s["story_id"] for s in after.json()["stories"]]
        assert str(story.id) not in listed_after

    @pytest.mark.asyncio
    async def test_pending_story_is_already_invisible_and_stays_invisible(
        self, app_with_db, db_session
    ):
        """The filter is what protects the surface, not the tool.

        A PENDING story is already absent, so unpublishing it is a real row change
        with a real log entry and zero visible effect. This is the test that would
        catch someone 'fixing' a route instead of the status.
        """
        story = _make_public_story(db_session, status=Story.Status.PENDING)
        await db_session.commit()
        client = TestClient(app_with_db)

        assert client.get(f"/stories/{story.id}").status_code == 404
        assert str(story.id) not in [
            s["story_id"] for s in client.get("/api/map/stories").json()["stories"]
        ]

        result = await unpublish_story(db_session, story.id, reason="closed out", actor="t")

        assert result.changed is True
        assert await _status_on_row(db_session, story.id) == Story.Status.REJECTED
        assert client.get(f"/stories/{story.id}").status_code == 404
        assert str(story.id) not in [
            s["story_id"] for s in client.get("/api/map/stories").json()["stories"]
        ]
        assert len(await _unpublish_logs(db_session)) == 1