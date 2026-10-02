"""Tests for the public map: auth boundary, default view, freshness stamp.

The map and the read only APIs behind it are anonymous. Curation, including
the queue and anything that reveals it, is not.
"""

import pytest
import uuid
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from src.shared.config import get_settings
from src.shared import database as database_module
from src.schema.models import (
    Event, RawArticle, ReportingUnit, SourceTier, Story, StoryUnitLink,
)


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


def _make_story(
    db_session,
    *,
    day,
    tier1_units=2,
    owners=2,
    events=0,
    tier1_sources=2,
    location="Kyiv",
    headline=None,
    status=Story.Status.QUEUED,
):
    """Create one story with a linked unit, article and optional events.

    QUEUED by default because the public surfaces only serve what a curator has
    approved; tests about the queue itself pass status=Story.Status.PENDING.
    """
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
        title=headline or f"Report from {location}",
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
            location_name=location,
            location_type="city",
            radius_km=25.0,
            start_time=day + timedelta(hours=index),
            event_type=Event.EventType.CONFLICT,
            confidence=0.8,
            source_count=tier1_sources,
            tier1_source_count=tier1_sources,
            entities={"GPE": [location]},
        ))

    return story


# Routes anonymous visitors may read. Kept as data so a new public route has to
# be added here on purpose rather than slipping in unnoticed.
PUBLIC_ROUTES = [
    "/map",
    "/api/globe/events",
    "/api/globe/stats",
    "/api/globe/layers",
    "/api/map/replay",
    "/api/map/freshness",
    "/api/map/stories",
]

# Curation surface. Every one of these must stay behind HTTP Basic auth.
AUTHED_ROUTES = [
    "/",
    "/posts",
]


class TestPublicRouteMatrix:
    """Anonymous GETs on the read only surface return 200."""

    @pytest.mark.parametrize("path", PUBLIC_ROUTES)
    @pytest.mark.asyncio
    async def test_anonymous_get_is_200(self, app_with_db, db_session, path):
        """Every public route answers an anonymous GET with 200."""
        now = datetime.now(timezone.utc)
        _make_story(db_session, day=now - timedelta(hours=2), events=1)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(path)
        assert response.status_code == 200, f"{path} returned {response.status_code}"
        assert "www-authenticate" not in response.headers

    @pytest.mark.asyncio
    async def test_public_html_routes_render(self, app_with_db, db_session):
        """/map renders its template for an anonymous reader."""
        now = datetime.now(timezone.utc)
        _make_story(db_session, day=now - timedelta(hours=2), events=1)
        await db_session.commit()

        client = TestClient(app_with_db)

        map_response = client.get("/map")
        assert map_response.status_code == 200
        assert "text/html" in map_response.headers.get("content-type", "")
        # Freshness stamp and the top stories list are server rendered.
        assert "map-stamp" in map_response.text
        assert "Top stories" in map_response.text

    @pytest.mark.asyncio
    async def test_public_story_page_renders_for_anonymous_reader(self, app_with_db, db_session):
        """GET /stories/{id} is public and shows sources, not curation state."""
        now = datetime.now(timezone.utc)
        story = _make_story(db_session, day=now - timedelta(hours=2), events=1)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/stories/{story.id}")
        assert response.status_code == 200
        assert "Report from Kyiv" in response.text
        # Curation internals never reach a public page.
        assert "gate_reason" not in response.text
        assert "PENDING" not in response.text

    @pytest.mark.asyncio
    async def test_public_story_page_404s_for_unknown_id(self, app_with_db, db_session):
        """An unknown story id is a 404, not a 500."""
        client = TestClient(app_with_db)
        response = client.get(f"/stories/{uuid.uuid4()}")
        assert response.status_code == 404


class TestAuthedRouteMatrix:
    """Curation routes must refuse anonymous callers with 401."""

    @pytest.mark.parametrize("path", AUTHED_ROUTES)
    @pytest.mark.asyncio
    async def test_anonymous_get_is_401(self, app_with_db, db_session, path):
        """Every curation page refuses an anonymous GET with 401."""
        now = datetime.now(timezone.utc)
        _make_story(db_session, day=now - timedelta(hours=2), events=1)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(path)
        assert response.status_code == 401, f"{path} returned {response.status_code}"
        assert "www-authenticate" in response.headers

    @pytest.mark.asyncio
    async def test_story_edit_requires_auth(self, app_with_db, db_session):
        """GET /story/{id}/edit is the curator's form, so it stays protected."""
        now = datetime.now(timezone.utc)
        story = _make_story(db_session, day=now - timedelta(hours=2),
                            status=Story.Status.PENDING)
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/story/{story.id}/edit")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_story_api_detail_endpoints_require_auth(self, app_with_db, db_session):
        """The viewpoints and sources APIs stay behind auth (see main.py for why)."""
        now = datetime.now(timezone.utc)
        story = _make_story(db_session, day=now - timedelta(hours=2),
                            status=Story.Status.PENDING)
        await db_session.commit()

        client = TestClient(app_with_db)
        for suffix in ("viewpoints", "sources"):
            response = client.get(f"/api/stories/{story.id}/{suffix}")
            assert response.status_code == 401, f"{suffix} was public"

    @pytest.mark.asyncio
    async def test_curation_posts_require_auth(self, app_with_db, db_session):
        """Approve, reject, save and mark posted all refuse anonymous POSTs."""
        now = datetime.now(timezone.utc)
        story = _make_story(db_session, day=now - timedelta(hours=2),
                            status=Story.Status.PENDING)
        await db_session.commit()

        from src.schema.models import CuratedPost

        post = CuratedPost(
            id=uuid.uuid4(),
            story_id=story.id,
            platform="twitter",
            caption="test",
            source_urls=[],
            status=CuratedPost.Status.APPROVED,
        )
        db_session.add(post)
        await db_session.commit()

        client = TestClient(app_with_db)
        cases = [
            (f"/story/{story.id}/approve", None),
            (f"/story/{story.id}/reject", None),
            (f"/story/{story.id}/save", {"caption": "a caption"}),
            (f"/post/{post.id}/mark-posted", None),
        ]
        for path, data in cases:
            response = client.post(path, data=data)
            assert response.status_code == 401, f"POST {path} was public"


class TestDefaultViewFilters:
    """The default map view is the recent, corroborated window."""

    @pytest.mark.asyncio
    async def test_events_endpoint_defaults_to_recent_corroborated(self, app_with_db, db_session):
        """With no filter params, only the last 48h corroborated events come back."""
        now = datetime.now(timezone.utc)
        # Inside the window and corroborated: returned.
        _make_story(
            db_session, day=now - timedelta(hours=5), events=1,
            tier1_sources=2, location="Recent",
        )
        # Inside the window but single sourced: filtered out by the gate.
        _make_story(
            db_session, day=now - timedelta(hours=5), events=1,
            tier1_sources=1, location="Uncorroborated",
        )
        # Corroborated but older than the window.
        _make_story(
            db_session, day=now - timedelta(hours=72), events=1,
            tier1_sources=3, location="Old",
        )
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get("/api/globe/events")
        assert response.status_code == 200
        names = {
            feature["properties"]["location_name"]
            for feature in response.json()["features"]
        }
        assert names == {"Recent"}

    @pytest.mark.asyncio
    async def test_events_endpoint_full_history_is_explicit(self, app_with_db, db_session):
        """hours=0 and min_tier1_sources=0 reach the whole, unfiltered history."""
        now = datetime.now(timezone.utc)
        _make_story(
            db_session, day=now - timedelta(hours=200), events=1,
            tier1_sources=1, location="Ancient",
        )
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get("/api/globe/events?hours=0&min_tier1_sources=0")
        assert response.status_code == 200
        names = {
            feature["properties"]["location_name"]
            for feature in response.json()["features"]
        }
        assert names == {"Ancient"}

    @pytest.mark.asyncio
    async def test_replay_endpoint_applies_corroboration_default(self, app_with_db, db_session):
        """/api/map/replay filters by the same threshold as the live view."""
        now = datetime.now(timezone.utc)
        _make_story(
            db_session, day=now - timedelta(hours=5), events=1,
            tier1_sources=2, location="Recent",
        )
        _make_story(
            db_session, day=now - timedelta(hours=5), events=1,
            tier1_sources=1, location="SingleSourced",
        )
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get("/api/map/replay")
        assert response.status_code == 200
        names = {
            feature["properties"]["location_name"]
            for feature in response.json()["features"]
        }
        assert names == {"Recent"}

        unfiltered = client.get("/api/map/replay?min_tier1_sources=0")
        assert unfiltered.status_code == 200
        assert len(unfiltered.json()["features"]) == 2

    @pytest.mark.asyncio
    async def test_replay_still_rejects_reversed_window(self, app_with_db, db_session):
        """The existing start/end validation is unchanged by the new params."""
        client = TestClient(app_with_db)
        response = client.get(
            "/api/map/replay?start=2026-01-02T00:00:00Z&end=2026-01-01T00:00:00Z"
        )
        assert response.status_code == 400


class TestTopStoriesList:
    """Top stories ranked by independent outlet count."""

    @pytest.mark.asyncio
    async def test_list_ranks_by_distinct_owners(self, app_with_db, db_session):
        """More independent owners ranks higher, and links to the public story."""
        now = datetime.now(timezone.utc)
        _make_story(db_session, day=now - timedelta(hours=3), owners=5, tier1_units=6, events=3,
                   headline="Ceasefire talks advance in Geneva")
        _make_story(db_session, day=now - timedelta(hours=3), owners=3, tier1_units=3, events=1,
                   headline="Harbor bridge reopens after inspection")
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get("/api/map/stories")
        assert response.status_code == 200
        payload = response.json()

        assert payload["count"] == 2
        owners = [story["outlets"] for story in payload["stories"]]
        assert owners == sorted(owners, reverse=True)
        assert payload["stories"][0]["outlets"] == 5
        # Corroboration uses the existing gate fields, and there is a public page.
        assert payload["stories"][0]["corroborated"] is True
        assert payload["stories"][0]["href"].startswith("/stories/")

    @pytest.mark.asyncio
    async def test_list_excludes_stories_below_the_gate(self, app_with_db, db_session):
        """A story with one owner fails the 2 owner gate and is not listed."""
        now = datetime.now(timezone.utc)
        _make_story(db_session, day=now - timedelta(hours=3), owners=1, tier1_units=1, events=2)
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/stories").json()
        assert payload["count"] == 0

        relaxed = client.get("/api/map/stories?min_owners=0").json()
        assert relaxed["count"] == 1
        assert relaxed["stories"][0]["corroborated"] is False

    @pytest.mark.asyncio
    async def test_list_respects_the_window(self, app_with_db, db_session):
        """Stories older than the default window drop out until asked for."""
        now = datetime.now(timezone.utc)
        _make_story(db_session, day=now - timedelta(hours=200), owners=4, tier1_units=4, events=1)
        await db_session.commit()

        client = TestClient(app_with_db)
        assert client.get("/api/map/stories").json()["count"] == 0
        assert client.get("/api/map/stories?hours=0").json()["count"] == 1

    @pytest.mark.asyncio
    async def test_list_omits_curation_internals(self, app_with_db, db_session):
        """No status, no gate reason and no queue language in the payload."""
        now = datetime.now(timezone.utc)
        story = _make_story(db_session, day=now - timedelta(hours=3), owners=4, events=1)
        story.gate_reason = "Blocked: single tier-1 owner group"
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/stories").json()
        assert "gate_reason" not in payload["stories"][0]
        assert "status" not in payload["stories"][0]
        assert "Blocked" not in client.get("/api/map/stories").text

    @pytest.mark.asyncio
    async def test_empty_list_is_a_count_not_a_crash(self, app_with_db, db_session):
        """An empty database gives an empty list, not an error."""
        client = TestClient(app_with_db)
        response = client.get("/api/map/stories")
        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 0
        assert payload["stories"] == []


class TestQueueIsNotPublic:
    """Only what a curator approved reaches the anonymous surfaces."""

    # Everything the pipeline can leave a story in that is not published.
    UNPUBLISHED = (
        Story.Status.PENDING,
        Story.Status.BLOCKED,
        Story.Status.REJECTED,
        Story.Status.EXPIRED,
    )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", UNPUBLISHED)
    async def test_public_story_page_404s_for_unpublished_story(
        self, app_with_db, db_session, status
    ):
        """The story page treats an unapproved story like an id that does not exist."""
        now = datetime.now(timezone.utc)
        story = _make_story(
            db_session, day=now - timedelta(hours=2), events=1, owners=4,
            status=status, headline=f"Only in the {status.value} queue",
        )
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/stories/{story.id}")
        assert response.status_code == 404
        assert "Only in the" not in response.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [Story.Status.QUEUED, Story.Status.POSTED])
    async def test_public_story_page_serves_approved_stories(
        self, app_with_db, db_session, status
    ):
        """An approved story still renders for an anonymous reader."""
        now = datetime.now(timezone.utc)
        story = _make_story(
            db_session, day=now - timedelta(hours=2), events=1, owners=4,
            status=status, headline=f"Approved: {status.value}",
        )
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/stories/{story.id}")
        assert response.status_code == 200
        assert f"Approved: {status.value}" in response.text

    @pytest.mark.asyncio
    async def test_top_stories_lists_only_approved_stories(self, app_with_db, db_session):
        """A well corroborated queue story never reaches /api/map/stories or /map."""
        now = datetime.now(timezone.utc)
        approved = _make_story(
            db_session, day=now - timedelta(hours=3), owners=5, tier1_units=6, events=3,
            headline="Ceasefire talks advance in Geneva",
        )
        # Same corroboration counts, so nothing but the status keeps them out.
        for status in TestQueueIsNotPublic.UNPUBLISHED:
            _make_story(
                db_session, day=now - timedelta(hours=3), owners=5, tier1_units=6,
                events=3, status=status, headline=f"Corroborated but {status.value}",
            )
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/stories").json()
        assert payload["count"] == 1
        assert payload["stories"][0]["href"] == f"/stories/{approved.id}"

        listing = client.get("/api/map/stories").text
        assert "Corroborated but" not in listing
        # The server-rendered /map list agrees with the JSON endpoint.
        assert str(approved.id) in client.get("/map").text
        assert "Corroborated but" not in client.get("/map").text


class TestEventLimitClamp:
    """Anonymous event payloads cannot ask for more rows than the cap."""

    @pytest.mark.asyncio
    async def test_limit_is_clamped_to_the_cap(self, app_with_db, db_session):
        """limit=100000000 with hours=0 is clamped, and the cap is reported back."""
        from curation_ui.main import MAP_EVENTS_MAX_LIMIT

        now = datetime.now(timezone.utc)
        for index in range(3):
            _make_story(
                db_session, day=now - timedelta(hours=2), events=1, tier1_sources=2,
                location=f"City{index}",
            )
        await db_session.commit()

        client = TestClient(app_with_db)

        huge = client.get("/api/globe/events?hours=0&limit=100000000").json()
        assert huge["limit"] == MAP_EVENTS_MAX_LIMIT
        assert huge["max_limit"] == MAP_EVENTS_MAX_LIMIT
        assert len(huge["features"]) == 3

        small = client.get("/api/globe/events?hours=0&limit=2").json()
        assert small["limit"] == 2
        assert len(small["features"]) == 2

        # A limit below 1 is clamped up, so a request never returns nothing by accident.
        zero = client.get("/api/globe/events?hours=0&limit=0").json()
        assert zero["limit"] == 1
        assert len(zero["features"]) == 1

    @pytest.mark.asyncio
    async def test_replay_shares_the_same_cap(self, app_with_db, db_session):
        """The replay endpoint reports the same cap the globe endpoint clamps to."""
        from curation_ui.main import MAP_EVENTS_MAX_LIMIT

        client = TestClient(app_with_db)
        payload = client.get("/api/map/replay?limit=100000000").json()
        assert payload["limit"] == MAP_EVENTS_MAX_LIMIT
        assert payload["max_limit"] == MAP_EVENTS_MAX_LIMIT


class TestContentSecurityPolicy:
    """Every response carries a CSP, and the inline scripts carry its nonce."""

    def test_header_is_present_on_pages_assets_and_apis(self, app_with_db):
        """CSP rides on HTML, static assets, JSON and the health probe alike."""
        client = TestClient(app_with_db)
        for path in ("/map", "/healthz", "/static/style.css",
                     "/static/errors.js", "/api/map/stories"):
            response = client.get(path)
            csp = response.headers.get("content-security-policy")
            assert csp, f"{path} was served without a CSP header"
            assert "default-src 'self'" in csp
            assert "object-src 'none'" in csp
            assert "frame-ancestors 'none'" in csp
            assert "form-action 'self'" in csp

    def test_inline_scripts_use_the_header_nonce_not_unsafe_inline(self, app_with_db):
        """The theme/keyboard handler is allowed by nonce, not by 'unsafe-inline'."""
        client = TestClient(app_with_db)

        response = client.get("/", auth=("testuser", "testpass"))
        assert response.status_code == 200
        csp = response.headers["content-security-policy"]
        script_src = next(part for part in csp.split("; ") if part.startswith("script-src"))
        assert "'unsafe-inline'" not in script_src
        assert "'unsafe-eval'" not in script_src

        nonce = script_src.split("'nonce-")[1].split("'")[0]
        assert f'<script nonce="{nonce}">' in response.text
        assert response.text.count("nonce=") == 1

        # Nonces are per response, so a leaked header cannot be replayed.
        second = client.get("/", auth=("testuser", "testpass"))
        assert second.headers["content-security-policy"] != csp


class TestFreshnessStamp:
    """The freshness stamp, from the pure formatter and from the endpoint."""

    def test_stamp_with_fresh_data(self):
        """A fresh, corroborated stamp reads as one line of plain facts."""
        from curation_ui.main import format_freshness_stamp

        now = datetime.now(timezone.utc)
        stamp = format_freshness_stamp(
            latest_at=now - timedelta(minutes=14),
            outlet_count=212,
            corroborated_events=38,
            now=now,
        )
        assert stamp == "Updated 14 min ago, 212 outlets, 38 corroborated events (24h)"

    def test_stamp_with_no_data(self):
        """No events at all says so instead of printing zero as a measurement."""
        from curation_ui.main import format_freshness_stamp

        now = datetime.now(timezone.utc)
        stamp = format_freshness_stamp(
            latest_at=None,
            outlet_count=0,
            corroborated_events=0,
            now=now,
        )
        assert stamp == "No events recorded yet"

    def test_stamp_with_stale_data(self):
        """Data that exists but is not corroborated in the window says both."""
        from curation_ui.main import format_freshness_stamp

        now = datetime.now(timezone.utc)
        stamp = format_freshness_stamp(
            latest_at=now - timedelta(days=3),
            outlet_count=40,
            corroborated_events=0,
            now=now,
        )
        assert stamp.startswith("No corroborated events (24h), 40 outlets, last event 3 days ago")
        assert "nan" not in stamp.lower()
        assert "none" not in stamp.lower()

    def test_stamp_never_reports_a_future_age(self):
        """A clock skew that puts an event in the future clamps to just now."""
        from curation_ui.main import format_freshness_stamp

        now = datetime.now(timezone.utc)
        stamp = format_freshness_stamp(
            latest_at=now + timedelta(hours=5),
            outlet_count=1,
            corroborated_events=1,
            now=now,
        )
        assert stamp.startswith("Updated just now")
        assert "-" not in stamp.split(",")[0].replace("Updated ", "")

    def test_stamp_handles_none_and_negative_counts(self):
        """None counters degrade to zero rather than crashing the page."""
        from curation_ui.main import format_freshness_stamp

        now = datetime.now(timezone.utc)
        stamp = format_freshness_stamp(
            latest_at=now - timedelta(minutes=5),
            outlet_count=None,
            corroborated_events=None,
            now=now,
        )
        assert stamp == "Updated 5 min ago, 0 outlets, 0 corroborated events (24h)"

    def test_stamp_tolerates_a_naive_timestamp(self):
        """A naive start_time from SQLite is read as UTC, not as an error."""
        from curation_ui.main import format_freshness_stamp

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        stamp = format_freshness_stamp(
            latest_at=now - timedelta(minutes=7),
            outlet_count=3,
            corroborated_events=1,
            now=now,
        )
        assert stamp.startswith("Updated 7 min ago")

    @pytest.mark.asyncio
    async def test_freshness_endpoint_on_empty_database(self, app_with_db, db_session):
        """An empty database still answers with an honest stamp."""
        client = TestClient(app_with_db)
        response = client.get("/api/map/freshness")
        assert response.status_code == 200
        payload = response.json()

        assert payload["corroborated_events"] == 0
        assert payload["outlets"] == 0
        assert payload["latest_at"] is None
        assert payload["updated_minutes_ago"] is None
        assert payload["stamp"] == "No events recorded yet"
        assert "nan" not in payload["stamp"].lower()

    @pytest.mark.asyncio
    async def test_freshness_endpoint_counts_outlets_and_events(self, app_with_db, db_session):
        """Outlet and corroborated event counts come from real rows."""
        now = datetime.now(timezone.utc)
        _make_story(db_session, day=now - timedelta(hours=4), events=3, tier1_sources=2)
        _make_story(db_session, day=now - timedelta(hours=4), events=1, tier1_sources=1)
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/freshness").json()

        assert payload["corroborated_events"] == 3
        assert payload["outlets"] == 2
        assert payload["updated_minutes_ago"] >= 0
        assert payload["stamp"].startswith("Updated ")

    @pytest.mark.asyncio
    async def test_freshness_endpoint_with_stale_data(self, app_with_db, db_session):
        """Stale rows report their age honestly and count zero corroborated."""
        now = datetime.now(timezone.utc)
        _make_story(db_session, day=now - timedelta(days=6), events=2, tier1_sources=4)
        await db_session.commit()

        client = TestClient(app_with_db)
        payload = client.get("/api/map/freshness").json()

        # No corroborated event inside the 24h window, but the outlets behind
        # the old story are still counted.
        assert payload["corroborated_events"] == 0
        assert payload["outlets"] == 1
        assert payload["updated_minutes_ago"] > 24 * 60
        assert payload["stamp"].startswith("No corroborated events (24h)")
