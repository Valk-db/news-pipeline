"""The /healthz/details verdict depends on transparency, and on nothing else.

curation_ui/health.py used to set the verdict inside the `try` that counted
`curated_posts` rows:

    try:
        async with get_session() as session:
            report["approved_posts"] = (await session.execute(text(
                "select count(*) from curated_posts where status = 'APPROVED'"
            ))).scalar()
        if <transparency is not ok>:
            report["verdict"] = "App and database are fine, but ..."
        else:
            report["verdict"] = "ok"
    except Exception:
        report["verdict"] = "curated_posts.status has the wrong enum type: run the SQL migration"

The count was never displayed anywhere, and the flow that produced it (approve /
reject / edit, removed 2026-10-02) is gone. What was left was a query whose only
remaining effect was to let a failure in it overwrite the real verdict with a
migration instruction about a column the app no longer reads.

These tests pin the property that was broken, in the strongest form available
here: the obsolete table is DROPPED from the live test database, so any code path
that still reaches for it raises, and the verdict must not move.
"""

import pytest
from sqlalchemy import event, text

from curation_ui import health
from src.shared import database as database_module

BEHIND = "App and database are fine, but transparency checkpoint signing is behind"


@pytest.fixture
async def details(db_engine, db_session, monkeypatch, test_settings):
    """Call healthz_details against the in-memory database, recording its SQL.

    Returns (call, statements) where `statements` is the list of SQL strings the
    handler actually executed, so a test can assert on what it queried rather
    than on what it returned.
    """
    # The transparency tables deliberately sit on their own metadata
    # (src/transparency/log.py), so conftest's create_all does not make them and
    # transparency_status() would report a missing table instead of a verdict.
    import src.transparency.store  # noqa: F401  (import registers the tables)
    from src.transparency.log import TransparencyBase

    async with db_engine.begin() as conn:
        await conn.run_sync(TransparencyBase.metadata.create_all)

    monkeypatch.setattr(database_module, "_engine", db_engine)
    monkeypatch.setattr(health, "get_settings", lambda: test_settings)

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", record)
    try:
        async def call():
            return await health.healthz_details(user="curator")

        yield call, statements
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", record)


class TestTheVerdictIsNotHijackableByADeadQuery:
    @pytest.mark.asyncio
    async def test_a_missing_curated_posts_table_does_not_change_the_verdict(
        self, details, db_session
    ):
        """The regression itself: the obsolete query cannot reach the verdict.

        On the pre-fix code this dropped the table and read back
        "curated_posts.status has the wrong enum type: run the SQL migration".
        """
        call, _ = details
        with_table = await call()
        await db_session.execute(text("drop table curated_posts"))
        await db_session.commit()
        without_table = await call()

        assert with_table["verdict"] == BEHIND
        assert without_table["verdict"] == with_table["verdict"]
        assert "curated_posts" not in without_table

    @pytest.mark.asyncio
    async def test_the_details_report_never_selects_from_curated_posts(
        self, details, db_session
    ):
        """The class guard: no query in this handler may name that table again.

        Asserting on the executed SQL rather than on the returned report is what
        makes this durable -- a query whose result is never displayed cannot be
        caught by looking at the output, which is exactly how this one survived.
        """
        call, statements = details
        await call()
        offenders = [s for s in statements if "curated_posts" in s.lower()]
        assert offenders == []


class TestTheLiveVerdictBranches:
    """Removing the dead block must not remove the branch that was live."""

    @pytest.mark.asyncio
    async def test_a_stale_transparency_verdict_reports_signing_behind(
        self, details, monkeypatch
    ):
        call, _ = details
        monkeypatch.setattr(
            health,
            "transparency_status",
            _stub_status({"verdict": "unhealthy", "reason": "18h old"}),
        )
        report = await call()
        assert report["verdict"] == BEHIND

    @pytest.mark.asyncio
    async def test_a_fresh_transparency_verdict_reports_ok(self, details, monkeypatch):
        call, _ = details
        monkeypatch.setattr(health, "transparency_status", _stub_status({"verdict": "ok"}))
        report = await call()
        assert report["verdict"] == "ok"

    @pytest.mark.asyncio
    async def test_ok_survives_the_obsolete_table_being_gone(
        self, details, db_session, monkeypatch
    ):
        """The ok branch reached through the dead try is the one that regressed."""
        call, _ = details
        monkeypatch.setattr(health, "transparency_status", _stub_status({"verdict": "ok"}))
        await db_session.execute(text("drop table curated_posts"))
        await db_session.commit()
        assert (await call())["verdict"] == "ok"

    @pytest.mark.asyncio
    async def test_the_story_status_breakdown_is_still_reported(self, details):
        """The neighbouring live query stays; only the dead one went."""
        call, _ = details
        report = await call()
        assert report["stories_by_status"] == {}
        assert report["db_connect"] == "ok"

    @pytest.mark.asyncio
    async def test_a_transparency_failure_string_is_not_read_as_a_dict(
        self, details, monkeypatch
    ):
        """A failed transparency probe leaves a string, not a dict.

        Unchanged behaviour, pinned because it is the branch that made the old
        code's `isinstance(..., dict)` check necessary.
        """
        call, _ = details
        monkeypatch.setattr(health, "transparency_status", _raise_status(RuntimeError("no db")))
        report = await call()
        assert isinstance(report["transparency"], str)
        assert report["verdict"] == "ok"


def _stub_status(payload: dict):
    async def _status() -> dict:
        return dict(payload)

    return _status


def _raise_status(exc: Exception):
    async def _status():
        raise exc

    return _status