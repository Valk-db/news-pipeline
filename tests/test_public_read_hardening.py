"""Hardening for the anonymous read API: every public surface is bounded, and a
misconfigured deployment does not describe itself to a stranger.

P2 clamped /api/globe/events after finding an unclamped limit on an
unauthenticated endpoint, and filtered the public story reads to approved
stories. These tests pin the follow-ups:

  - /api/globe/layers and /stories/{id} were the two public routes still able to
    fetch an unbounded number of rows.
  - /api/map/stories echoed the requested window back but not the clamped one, so
    an anonymous caller asking for hours=999999 saw its own request rather than
    what it got.
  - every anonymous route rendered the curator's diagnostic when the database was
    unconfigured, which names the environment variable to set.

The route table in tests/test_route_table.py decides what is anonymous; this file
assumes it and tests what those routes do.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from curation_ui.app_state import (
    DATABASE_UNAVAILABLE_PUBLIC,
    check_database_public,
)
from curation_ui.globe import MAP_LAYERS_MAX_LIMIT
from curation_ui.public_pages import PUBLIC_STORY_EVENT_CAP, PUBLIC_STORY_UNIT_CAP
from src.schema.models import (
    Event,
    EventLayer,
    RawArticle,
    ReportingUnit,
    SourceTier,
    Story,
    StoryUnitLink,
)
from src.shared import database as database_module
from src.shared.config import get_settings


@pytest.fixture
def test_settings(monkeypatch):
    """Configure test settings with curation auth enabled."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app_with_db(test_settings, db_engine):
    """Create app backed by the shared in-memory test database."""
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    from curation_ui.main import app

    main_module.settings = test_settings

    import src.shared.llm as llm_module
    llm_module._llm_client = None

    return app


@pytest.fixture
def app_without_db(monkeypatch):
    """The app with settings that have no database configured at all."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    get_settings.cache_clear()

    import curation_ui.main as main_module
    from curation_ui.main import app

    class _NoDatabase:
        has_database = False
        has_llm = False
        has_curation_auth = True

    main_module.settings = _NoDatabase()
    return app


def _make_story(db_session, *, day, tier1_units=2, owners=2, events=0, status=Story.Status.QUEUED):
    """One approved story with a linked unit and article, plus its events."""
    story = Story(
        id=uuid.uuid4(),
        day=day,
        primary_entities=["test-entity"],
        status=status,
        tier1_unit_count=tier1_units,
        tier2_unit_count=0,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=owners,
    )
    db_session.add(story)

    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=day,
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
    )
    article = RawArticle(
        id=unit.representative_article_id,
        url=f"https://example.com/{unit.id}",
        url_hash=str(unit.id).replace("-", ""),
        title=f"Report {story.id}",
        source_domain="example.com",
        source_tier=SourceTier.TIER1,
        reporting_unit_id=unit.id,
    )
    db_session.add(unit)
    db_session.add(article)
    db_session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))

    for index in range(events):
        db_session.add(Event(
            id=uuid.uuid4(),
            story_id=story.id,
            latitude=50.45,
            longitude=30.52,
            location_name="Kyiv",
            location_type="city",
            radius_km=25.0,
            start_time=day + timedelta(hours=index),
            event_type=Event.EventType.CONFLICT,
            confidence=0.8,
            source_count=2,
            tier1_source_count=2,
            entities={"GPE": ["Kyiv"]},
        ))

    return story


def _add_units(db_session, story, count, day):
    """Attach `count` further reporting units with articles to a story."""
    for index in range(count):
        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=day,
            representative_article_id=uuid.uuid4(),
            article_count=1,
            source_tiers={"tier1": 1},
            owner_groups={f"OWNER{index}": 1},
            tier1_owner_groups={f"OWNER{index}": 1},
        )
        article = RawArticle(
            id=unit.representative_article_id,
            url=f"https://example.com/{unit.id}",
            url_hash=str(unit.id).replace("-", ""),
            title=f"Extra source {index}",
            source_domain="example.com",
            source_tier=SourceTier.TIER1,
            reporting_unit_id=unit.id,
        )
        db_session.add(unit)
        db_session.add(article)
        db_session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))


# Every route an anonymous caller can reach. Kept in one place so a new public
# route has to be hardened deliberately rather than by omission. The HTML
# surfaces answer 503 when the database is unconfigured (render_error_page's
# contract, unchanged); the JSON surfaces answer 200 with {"error": ...}.
ANONYMOUS_SURFACES = [
    "/map",
    "/api/globe/events",
    "/api/globe/stats",
    "/api/globe/layers",
    "/api/map/freshness",
    "/api/map/stories",
    "/api/map/replay",
]

ANONYMOUS_HTML_SURFACES = [path for path in ANONYMOUS_SURFACES if not path.startswith("/api/")]
ANONYMOUS_JSON_SURFACES = [path for path in ANONYMOUS_SURFACES if path.startswith("/api/")]


class TestGlobeLayersAreCapped:
    """The layer list is an anonymous read of a table nothing bounds."""

    @pytest.mark.asyncio
    async def test_reports_its_cap(self, app_with_db, db_session):
        """A short list comes back whole, with the cap visible like P2's events."""
        db_session.add(EventLayer(
            id=uuid.uuid4(),
            name="conflict",
            description="Armed conflicts",
            is_default=True,
            is_visible=True,
            color="#e74c3c",
        ))
        await db_session.commit()

        response = TestClient(app_with_db).get("/api/globe/layers")
        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 1
        assert payload["limit"] == MAP_LAYERS_MAX_LIMIT
        assert payload["max_limit"] == MAP_LAYERS_MAX_LIMIT
        assert [layer["name"] for layer in payload["layers"]] == ["conflict"]

    @pytest.mark.asyncio
    async def test_never_returns_more_than_the_cap(self, app_with_db, db_session, monkeypatch):
        """Past the cap the response stops at the cap rather than growing."""
        monkeypatch.setattr("curation_ui.globe.MAP_LAYERS_MAX_LIMIT", 2)
        for index in range(5):
            db_session.add(EventLayer(id=uuid.uuid4(), name=f"layer-{index}"))
        await db_session.commit()

        payload = TestClient(app_with_db).get("/api/globe/layers").json()
        assert payload["count"] == 2
        assert len(payload["layers"]) == 2
        assert payload["limit"] == 2
        assert payload["max_limit"] == 2

    def test_the_cap_is_a_real_number(self):
        assert isinstance(MAP_LAYERS_MAX_LIMIT, int)
        assert 0 < MAP_LAYERS_MAX_LIMIT <= 1000


class TestMapStoriesReportsTheWindowItActuallyUsed:
    """P2's lesson applied to hours: the echo was the request, not the result."""

    @pytest.mark.asyncio
    async def test_requested_window_is_echoed_and_clamp_is_reported(self, app_with_db, db_session):
        _make_story(db_session, day=datetime.now(timezone.utc) - timedelta(hours=2), events=1)
        await db_session.commit()

        payload = TestClient(app_with_db).get("/api/map/stories?hours=48").json()
        assert payload["window_hours"] == 48
        assert payload["effective_window_hours"] == 48

    @pytest.mark.asyncio
    async def test_an_oversized_window_is_reported_as_clamped(self, app_with_db, db_session):
        """hours=999999 was served a MAP_MAX_WINDOW_HOURS window; say so."""
        from curation_ui.discovery import MAP_MAX_WINDOW_HOURS

        _make_story(db_session, day=datetime.now(timezone.utc) - timedelta(hours=2), events=1)
        await db_session.commit()

        payload = TestClient(app_with_db).get("/api/map/stories?hours=999999").json()
        assert payload["window_hours"] == 999999
        assert payload["effective_window_hours"] == MAP_MAX_WINDOW_HOURS

    @pytest.mark.asyncio
    async def test_unbounded_window_reports_none(self, app_with_db, db_session):
        """hours=0 means unbounded, which is not a number of hours."""
        _make_story(db_session, day=datetime.now(timezone.utc) - timedelta(hours=2), events=1)
        await db_session.commit()

        payload = TestClient(app_with_db).get("/api/map/stories?hours=0").json()
        assert payload["effective_window_hours"] is None

    @pytest.mark.asyncio
    async def test_explicit_window_wins_over_hours(self, app_with_db, db_session):
        """An explicit start/end pair is the whole story of the window."""
        _make_story(db_session, day=datetime.now(timezone.utc) - timedelta(hours=2), events=1)
        await db_session.commit()

        start = "2026-09-30T00:00:00Z"
        end = "2026-09-30T06:00:00Z"
        payload = TestClient(app_with_db).get(
            f"/api/map/stories?hours=999999&start={start}&end={end}"
        ).json()
        assert payload["effective_window_hours"] == 6

    @pytest.mark.asyncio
    async def test_a_half_open_window_is_reported_as_unbounded(self, app_with_db, db_session):
        """start without end has no width to report; None is the honest answer."""
        _make_story(db_session, day=datetime.now(timezone.utc) - timedelta(hours=2), events=1)
        await db_session.commit()

        payload = TestClient(app_with_db).get(
            "/api/map/stories?start=2026-09-30T00:00:00Z"
        ).json()
        assert payload["effective_window_hours"] is None


class TestPublicStoryPageIsCapped:
    """The story page needs no query parameter to reach a big story."""

    @pytest.mark.asyncio
    async def test_a_short_story_is_whole_and_says_nothing_about_caps(self, app_with_db, db_session):
        story = _make_story(db_session, day=datetime.now(timezone.utc) - timedelta(hours=2), events=1)
        await db_session.commit()

        response = TestClient(app_with_db).get(f"/stories/{story.id}")
        assert response.status_code == 200
        assert "1 reporting unit" in response.text
        assert "Truncated for display" not in response.text

    @pytest.mark.asyncio
    async def test_units_past_the_cap_are_cut_and_announced(
        self, app_with_db, db_session, monkeypatch
    ):
        monkeypatch.setattr("curation_ui.public_pages.PUBLIC_STORY_UNIT_CAP", 3)
        day = datetime.now(timezone.utc) - timedelta(hours=2)
        story = _make_story(db_session, day=day, events=0)
        _add_units(db_session, story, 5, day)
        await db_session.commit()

        response = TestClient(app_with_db).get(f"/stories/{story.id}")
        assert response.status_code == 200
        # The header states the true size even though the list was cut.
        assert "6 reporting units" in response.text
        # Three sources rendered (the cap), not all six, and the page admits it.
        assert response.text.count('<li class="story-public-source">') == 3
        assert "Truncated for display: 3 of 6 sources." in response.text

    @pytest.mark.asyncio
    async def test_events_past_the_cap_are_cut_and_announced(
        self, app_with_db, db_session, monkeypatch
    ):
        monkeypatch.setattr("curation_ui.public_pages.PUBLIC_STORY_EVENT_CAP", 2)
        day = datetime.now(timezone.utc) - timedelta(hours=2)
        story = _make_story(db_session, day=day, events=5)
        await db_session.commit()

        response = TestClient(app_with_db).get(f"/stories/{story.id}")
        assert response.status_code == 200
        assert response.text.count('<li class="story-public-event">') == 2
        assert (
            "Truncated for display: 1 of 1 source, "
            "and the 2 most recent of this story's events."
        ) in response.text

    @pytest.mark.asyncio
    async def test_the_newest_events_are_the_ones_kept(
        self, app_with_db, db_session, monkeypatch
    ):
        """The cap drops the oldest: the newest event is on the page, day+0h is not."""
        monkeypatch.setattr("curation_ui.public_pages.PUBLIC_STORY_EVENT_CAP", 2)
        day = datetime.now(timezone.utc) - timedelta(hours=10)
        story = _make_story(db_session, day=day, events=6)
        await db_session.commit()

        text = TestClient(app_with_db).get(f"/stories/{story.id}").text
        # events run day+0h .. day+5h; the two newest survive the cap.
        assert (day + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M UTC") in text
        assert (day + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M UTC") in text
        assert (day + timedelta(hours=0)).strftime("%Y-%m-%d %H:%M UTC") not in text
        assert "Truncated for display: 1 of 1 source," in text

    def test_the_caps_are_real_numbers(self):
        for cap in (PUBLIC_STORY_UNIT_CAP, PUBLIC_STORY_EVENT_CAP):
            assert isinstance(cap, int)
            assert 0 < cap <= 1000


class TestAnonymousSurfacesDoNotDescribeTheDeployment:
    """A misconfigured deployment should not publish which knob is missing."""

    @pytest.mark.parametrize("path", ANONYMOUS_SURFACES)
    def test_no_anonymous_route_names_an_environment_variable(self, app_without_db, path):
        response = TestClient(app_without_db).get(path)
        # 200 for the JSON surfaces, 503 for the HTML ones (render_error_page).
        assert response.status_code in (200, 503), f"{path} returned {response.status_code}"
        body = response.text
        assert "DATABASE_URL" not in body, path
        assert "not configured" not in body, path

    @pytest.mark.parametrize("path", ANONYMOUS_JSON_SURFACES)
    def test_json_surfaces_carry_the_generic_error(self, app_without_db, path):
        payload = TestClient(app_without_db).get(path).json()
        assert payload["error"] == DATABASE_UNAVAILABLE_PUBLIC

    @pytest.mark.parametrize("path", ANONYMOUS_HTML_SURFACES)
    def test_html_surfaces_render_the_generic_error_page(self, app_without_db, path):
        response = TestClient(app_without_db).get(path)
        assert response.status_code == 503
        assert DATABASE_UNAVAILABLE_PUBLIC in response.text
        assert "text/html" in response.headers.get("content-type", "")

    def test_the_public_message_names_nothing_internal(self):
        assert "DATABASE_URL" not in DATABASE_UNAVAILABLE_PUBLIC
        assert "GROQ" not in DATABASE_UNAVAILABLE_PUBLIC
        assert "CEREBRAS" not in DATABASE_UNAVAILABLE_PUBLIC

    def test_the_curator_routes_keep_the_diagnostic(self, app_without_db):
        """The triage pages still say what to set: the reader there can act on it."""
        import base64

        credentials = base64.b64encode(b"testuser:testpass").decode()
        response = TestClient(app_without_db).get(
            "/", headers={"Authorization": f"Basic {credentials}"}
        )
        assert response.status_code == 503
        assert "DATABASE_URL" in response.text

    def test_check_database_public_returns_the_same_verdict(self, app_with_db, test_settings):
        """The public variant only changes the message, never the availability."""
        class _Stub:
            def __init__(self, ok):
                self.ok = ok

            def check_database_available(self):
                return (self.ok, "Database not configured. Set DATABASE_URL environment variable.")

        class _Request:
            def __init__(self, stub):
                self.app = type("A", (), {"state": stub})()

        available, message = check_database_public(_Request(_Stub(True)))
        assert available is True
        assert message == ""

        available, message = check_database_public(_Request(_Stub(False)))
        assert available is False
        assert message == DATABASE_UNAVAILABLE_PUBLIC
        assert "DATABASE_URL" not in message