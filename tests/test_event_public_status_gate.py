"""The public-status rule on the event endpoints, pinned three ways.

`/api/globe/events` (anonymous, edge-cacheable) and `/api/map/replay` read the
same table through the same clause builder, `curation_ui.events._event_conditions`.
That builder produced predicates on the events table only, so both endpoints served
pins belonging to stories that are PENDING, BLOCKED or REJECTED while `/stories/{id}`
answered 404 for the same stories -- `scripts/unpublish_story.py` sets REJECTED and
the pin stayed on the map.

The rule is now built into `_event_conditions`, so this file pins it where a future
refactor has to look:

  * `TestCompiledWhereClause` asserts the compiled Postgres SQL with literal binds,
    because the symptom-level tests below would also pass if the endpoint answered for
    an unrelated reason, and "the endpoint returned nothing" is indistinguishable from
    "the endpoint is broken" without a positive control.
  * `TestAnonymousEndpointsHonourIt` drives the real anonymous requests, with a QUEUED
    story's pin as the positive control in every test.
  * `TestClauseBuilderConsumers` walks the AST of every product module so a caller that
    stops applying the returned list, or a third caller that appears, is a deliberate act.

SQLite cannot parse `EXPLAIN` and has no `pg_trgm`, so the shape of this fix is proven by
compiling the statement the way Postgres will receive it. The query plan and the leak size
are for the home machine; see the batch report.
"""

import ast
import uuid
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import and_, select
from sqlalchemy.dialects import postgresql

from curation_ui.discovery import PUBLIC_STORY_STATUSES, _apply_story_tier_filter, _parse_tiers
from curation_ui.events import _event_conditions
from src.schema.models import Event, RawArticle, ReportingUnit, SourceTier, Story, StoryUnitLink
from src.shared import database as database_module
from src.shared.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

# The literal the compiled clause must carry: the Postgres enum stores member NAMES
# (`CREATE TYPE status AS ENUM ('PENDING', 'QUEUED', ...)`), not the lowercase values.
EXPECTED_STATUS_LITERAL = "('QUEUED', 'POSTED')"


@pytest.fixture
def test_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app_with_db(test_settings, db_engine):
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    from curation_ui.main import app

    main_module.settings = test_settings

    import src.shared.llm as llm_module

    llm_module._llm_client = None
    return app


def _story_with_event(db_session, *, status, location):
    """One story in `status` owning exactly one event, everything else irrelevant.

    tier1_source_count is high and the event sits inside the default window so the
    corroboration filter and the time window both pass: the only thing separating this
    event from the response is the status rule under test.
    """
    now = datetime.now(UTC)
    story = Story(
        id=uuid.uuid4(),
        day=now - timedelta(hours=2),
        primary_entities=["test-entity"],
        status=status,
        tier1_unit_count=2,
        distinct_owners=2,
    )
    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=now - timedelta(hours=2),
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
    )
    db_session.add_all([
        story,
        unit,
        RawArticle(
            id=unit.representative_article_id,
            url=f"https://example.com/{unit.id}",
            url_hash=str(unit.id).replace("-", ""),
            title=f"Report from {location}",
            source_domain="example.com",
            source_tier=SourceTier.TIER1,
            reporting_unit_id=unit.id,
        ),
        StoryUnitLink(story_id=story.id, unit_id=unit.id),
        Event(
            id=uuid.uuid4(),
            story_id=story.id,
            latitude=50.45,
            longitude=30.52,
            location_name=location,
            location_type="city",
            radius_km=25.0,
            start_time=now - timedelta(hours=1),
            event_type=Event.EventType.CONFLICT,
            confidence=0.9,
            source_count=4,
            tier1_source_count=4,
        ),
    ])
    return story


def _served_locations(client, path) -> set[str]:
    response = client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"
    return {
        feature["properties"]["location_name"]
        for feature in response.json()["features"]
    }


class TestCompiledWhereClause:
    """The WHERE clause Postgres receives, with every value rendered as a literal."""

    @staticmethod
    def _sql(stmt) -> str:
        sql = str(stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        ))
        return " ".join(sql.split())

    @staticmethod
    def _where(stmt) -> str:
        return TestCompiledWhereClause._sql(stmt).split(" WHERE ", 1)[1]

    def test_the_clause_builder_filters_on_public_story_status(self):
        where = self._where(select(Event).where(and_(*_event_conditions())))
        assert (
            "events.story_id IN (SELECT stories.id FROM stories "
            f"WHERE stories.status IN {EXPECTED_STATUS_LITERAL})"
        ) in where

    def test_the_rule_is_present_with_every_optional_filter_switched_on(self):
        """Not a no-argument special case: it survives the fullest call."""
        conditions = _event_conditions(
            layer_id=uuid.uuid4(),
            event_type=Event.EventType.CONFLICT,
            min_confidence=0.5,
            bbox="-10,-10,10,10",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
            min_tier1_sources=2,
        )
        where = self._where(select(Event).where(and_(*conditions)))
        assert f"stories.status IN {EXPECTED_STATUS_LITERAL}" in where
        assert "events.event_type" in where
        assert "events.layer_id" in where
        assert "events.start_time" in where

    def test_the_literal_matches_the_rule_it_is_built_from(self):
        """If PUBLIC_STORY_STATUSES ever changes, this names the old value."""
        assert PUBLIC_STORY_STATUSES == (Story.Status.QUEUED, Story.Status.POSTED)
        assert EXPECTED_STATUS_LITERAL == "('QUEUED', 'POSTED')"

    def test_the_rule_survives_the_tier_join_the_endpoints_also_apply(self):
        """globe.py applies _apply_story_tier_filter as well; both must coexist.

        A JOIN on stories was rejected for this reason and the semi-join was used
        instead: this asserts the compiled statement still names one stories and the
        status rule, i.e. no alias collision and no silently dropped predicate.
        """
        stmt = select(Event)
        stmt = _apply_story_tier_filter(stmt, _parse_tiers("1"))
        stmt = stmt.where(and_(*_event_conditions()))
        sql = self._sql(stmt)
        assert "JOIN stories ON" in sql
        assert f"stories.status IN {EXPECTED_STATUS_LITERAL}" in sql

    def test_the_clause_names_only_the_two_public_statuses(self):
        """REJECTED, BLOCKED, PENDING and EXPIRED must not appear as servable."""
        where = self._where(select(Event).where(and_(*_event_conditions())))
        for forbidden in ("REJECTED", "BLOCKED", "PENDING", "EXPIRED"):
            assert forbidden not in where


class TestAnonymousEndpointsHonourIt:
    """The real anonymous requests, each with a QUEUED positive control."""

    @pytest.mark.parametrize("status", [
        Story.Status.REJECTED,
        Story.Status.BLOCKED,
        Story.Status.PENDING,
        Story.Status.EXPIRED,
    ])
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path,query", [
        ("/api/globe/events", "?hours=0&min_tier1_sources=0"),
        ("/api/map/replay", "?min_tier1_sources=0"),
    ])
    async def test_non_public_pin_is_not_served(self, app_with_db, db_session, status, path, query):
        """The retraction path: a non-public story keeps its pin off both surfaces.

        The positive control runs in the same database in the same request, so a
        response containing only the control cannot be confused with a broken endpoint.
        """
        _story_with_event(db_session, status=status, location="HiddenPin")
        _story_with_event(db_session, status=Story.Status.QUEUED, location="PublicPin")
        await db_session.commit()

        client = TestClient(app_with_db)
        assert _served_locations(client, path + query) == {"PublicPin"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path,query", [
        ("/api/globe/events", "?hours=0&min_tier1_sources=0"),
        ("/api/map/replay", "?min_tier1_sources=0"),
    ])
    async def test_public_pin_is_served_with_the_default_filters(self, app_with_db, db_session, path, query):
        """The endpoint still works with no query string at all: the default window
        and corroboration threshold must not be what hides the non-public rows."""
        _story_with_event(db_session, status=Story.Status.QUEUED, location="PublicPin")
        _story_with_event(db_session, status=Story.Status.REJECTED, location="HiddenPin")
        await db_session.commit()

        client = TestClient(app_with_db)
        assert _served_locations(client, path) == {"PublicPin"}

    @pytest.mark.asyncio
    async def test_every_public_status_is_served(self, app_with_db, db_session):
        """POSTED is public too; a gate that only honours QUEUED would pass the rest."""
        _story_with_event(db_session, status=Story.Status.QUEUED, location="Queued")
        _story_with_event(db_session, status=Story.Status.POSTED, location="Posted")
        await db_session.commit()

        client = TestClient(app_with_db)
        assert _served_locations(client, "/api/globe/events?hours=0&min_tier1_sources=0") == {
            "Queued", "Posted",
        }

    @pytest.mark.asyncio
    async def test_the_story_page_and_the_map_agree(self, app_with_db, db_session):
        """The drift itself: /stories/{id} 404s a REJECTED story and the map must too."""
        story = _story_with_event(db_session, status=Story.Status.REJECTED, location="HiddenPin")
        public = _story_with_event(db_session, status=Story.Status.QUEUED, location="PublicPin")
        await db_session.commit()

        client = TestClient(app_with_db)
        assert client.get(f"/stories/{story.id}").status_code == 404
        assert client.get(f"/stories/{public.id}").status_code == 200
        assert _served_locations(client, "/api/globe/events?hours=0&min_tier1_sources=0") == {
            "PublicPin",
        }


KNOWN_CONSUMERS = {
    ("curation_ui/globe.py", "get_globe_events"),
    ("curation_ui/map_api.py", "get_map_replay"),
}


def _clause_builder_call_sites():
    """(file, enclosing function) for every _event_conditions call in the product."""
    sites = []
    for path in sorted((REPO_ROOT / "curation_ui").glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [
                call for call in ast.walk(node)
                if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "_event_conditions"
            ]
            for _ in calls:
                sites.append((f"curation_ui/{path.name}", node.name, node))
    return sites


def _applies_verbatim(function_node) -> bool:
    """True when the list _event_conditions returns reaches stmt.where(and_(...)).

    A caller that drops a condition, or filters the list, would keep the function
    alive and quietly re-open the hole; so would one that never passes the list to a
    WHERE at all.
    """
    names = set()
    for node in ast.walk(function_node):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if getattr(node.value.func, "id", None) != "_event_conditions":
            continue
        names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    if not names:
        return False
    for call in ast.walk(function_node):
        if not (isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "where"):
            continue
        for arg in call.args:
            # stmt.where(*conditions) or stmt.where(and_(*conditions))
            if isinstance(arg, ast.Name) and arg.id in names:
                return True
            for inner in ast.walk(arg):
                if isinstance(inner, ast.Starred) and isinstance(inner.value, ast.Name):
                    if inner.value.id in names:
                        return True
    return False


class TestClauseBuilderConsumers:
    """Every consumer of the shared builder, enumerated rather than assumed.

    A grep finds the places that USE the rule; this finds the places that consume the
    builder, which is the set that can opt out.
    """

    def test_the_consumer_list_is_exactly_the_two_known_endpoints(self):
        found = {(f, fn) for f, fn, _ in _clause_builder_call_sites()}
        assert found == KNOWN_CONSUMERS, (
            f"_event_conditions consumers changed: {sorted(found)}. A new consumer "
            "inherits the public-status rule from the builder, so add it here "
            "deliberately."
        )

    def test_every_consumer_applies_the_returned_conditions_verbatim(self):
        offenders = [
            f"{f}:{fn}" for f, fn, node in _clause_builder_call_sites()
            if not _applies_verbatim(node)
        ]
        assert not offenders, (
            f"these consumers do not pass the whole list to a WHERE clause: {offenders}"
        )