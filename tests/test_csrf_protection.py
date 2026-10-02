"""The CSRF primitive, and the fact that no route currently needs it.

This module used to be the CSRF suite for the four curation triage routes: approve,
reject, save and mark-posted. Those routes are deleted (the curation queue is
read-only now; see `tests/test_route_table.py` for the route table contract), so
`require_csrf` and `issue_csrf_token` have no production consumer left. They stay in
`curation_ui/security.py` as a security primitive for the next flow that needs them,
and they stay tested here.

The tests are split in two on purpose:

* `TestRequireCsrfPrimitive` exercises `require_csrf` against a throwaway app that
  mounts it as a dependency. That is a real exercise of the primitive, not a
  simulation: a route that adopts it later gets the behaviour proven below.
* `TestNoRouteNeedsCsrfToday` pins the current state of the actual app: nothing is
  CSRF-guarded, and no page mints a token any more.

The rationale the original docstring recorded still holds and is worth keeping in the
code: browsers cache HTTP Basic credentials and re-attach them to cross-origin
requests, so the day a mutating route comes back it must require this token too.
"""

import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from src.schema.models import RawArticle, ReportingUnit, SourceTier, Story, StoryUnitLink
from src.shared import database as database_module
from src.shared.config import get_settings

AUTH = ("testuser", "testpass")


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
    import curation_ui.security as security_module
    from curation_ui.main import app

    main_module.settings = test_settings

    import src.shared.llm as llm_module
    llm_module._llm_client = None
    security_module.auth_limiter.reset()
    return app


@pytest.fixture
async def pending_story(db_session):
    """One PENDING story with a linked unit and article."""
    now = datetime.now(timezone.utc)
    story = Story(
        id=uuid.uuid4(),
        day=now,
        primary_entities=["test-entity"],
        status=Story.Status.PENDING,
        tier1_unit_count=2,
        distinct_owners=2,
    )
    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=now,
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
    )
    article = RawArticle(
        id=unit.representative_article_id,
        url="https://example.com/report",
        url_hash=str(unit.id).replace("-", ""),
        title="Officials described the situation in a short statement",
        source_domain="example.com",
        source_tier=SourceTier.TIER1,
        reporting_unit_id=unit.id,
    )
    db_session.add_all([story, unit, article, StoryUnitLink(story_id=story.id, unit_id=unit.id)])
    await db_session.commit()
    return story


def _csrf_headers():
    from curation_ui.security import CSRF_HEADER, issue_csrf_token
    return {CSRF_HEADER: issue_csrf_token()}


@pytest.fixture
def guarded_app(test_settings):
    """A throwaway app with one route that does require the token.

    This is what `require_csrf` is for, and it is the only way to test the primitive
    honestly now that no real route uses it.
    """
    from curation_ui.security import require_csrf

    app = FastAPI()

    @app.post("/mutate")
    async def mutate(_token=Depends(require_csrf)):
        return {"ok": True}

    @app.get("/read")
    async def read(request: Request):
        return {"ok": True}

    return app


class TestRequireCsrfPrimitive:
    """A POST that does not carry this app's token is refused."""

    @pytest.mark.parametrize("headers", [
        None,
        {},
        {"X-CSRF-Token": ""},
        {"X-CSRF-Token": "not-a-token"},
        {"X-CSRF-Token": "abc.def"},  # right shape, unsigned
    ])
    def test_post_without_a_valid_token_is_403(self, guarded_app, headers):
        response = TestClient(guarded_app).post("/mutate", headers=headers)
        assert response.status_code == 403
        assert "CSRF" in response.json()["detail"]

    def test_a_valid_token_is_accepted(self, guarded_app):
        response = TestClient(guarded_app).post("/mutate", headers=_csrf_headers())
        assert response.status_code == 200

    def test_a_tampered_token_is_refused(self, guarded_app):
        from curation_ui.security import CSRF_HEADER, issue_csrf_token

        response = TestClient(guarded_app).post(
            "/mutate", headers={CSRF_HEADER: issue_csrf_token() + "x"}
        )
        assert response.status_code == 403

    def test_rotating_the_password_invalidates_outstanding_tokens(self, guarded_app, monkeypatch):
        """A tab that was open before the credentials rotated cannot mutate anything."""
        from curation_ui.security import CSRF_HEADER, issue_csrf_token

        stale = {CSRF_HEADER: issue_csrf_token()}
        monkeypatch.setenv("CURATION_PASSWORD", "rotated-password")
        get_settings.cache_clear()

        client = TestClient(guarded_app)
        assert client.post("/mutate", headers=stale).status_code == 403
        assert client.post("/mutate", headers=_csrf_headers()).status_code == 200

    def test_reads_never_need_a_token(self, guarded_app):
        """The guard is on the mutating route only, never on a read."""
        assert TestClient(guarded_app).get("/read").status_code == 200


class TestNoRouteNeedsCsrfToday:
    """The current state of the real app, pinned."""

    def test_no_route_is_csrf_guarded(self, app_with_db):
        """The four routes that used to be guarded are gone; nothing replaced them.

        This reuses the route walker from test_route_table.py, which knows how to
        descend into FastAPI's nested _IncludedRouter objects. Walking app.routes
        directly would find nothing and pass vacuously.
        """
        from fastapi.routing import APIRoute

        from tests.test_route_table import dependency_names, iter_routes

        guarded = [
            route.path
            for route in iter_routes(app_with_db.routes)
            if isinstance(route, APIRoute)
            and "require_csrf" in dependency_names(route.dependant)
        ]
        assert guarded == [], f"CSRF-guarded routes exist: {guarded}"

    def test_the_curation_queue_renders_without_a_token(self, app_with_db):
        """The queue is read-only, so it mints no token and sends no token header."""
        client = TestClient(app_with_db)
        for path in ("/", "/map"):
            response = client.get(path, auth=AUTH)
            assert response.status_code == 200
            assert "X-CSRF-Token" not in response.text
            assert "csrf_token" not in response.text

    def test_public_pages_carry_no_token(self, app_with_db):
        """An anonymous page must not hand out a token."""
        client = TestClient(app_with_db)
        for path in ("/map", "/stories/" + str(uuid.uuid4())):
            response = client.get(path)
            assert "csrf_token" not in response.text
            assert "X-CSRF-Token" not in response.text


class TestTokenProperties:
    """Unit level: what the token is and is not."""

    def test_tokens_are_unique_per_call(self):
        from curation_ui.security import issue_csrf_token

        assert len({issue_csrf_token() for _ in range(20)}) == 20

    def test_the_header_name_is_the_one_the_templates_used_to_send(self):
        """If this constant ever changes, the htmx attribute that used to carry it
        would silently stop matching, so pin the wire format."""
        from curation_ui.security import CSRF_HEADER

        assert CSRF_HEADER == "X-CSRF-Token"
        assert re.fullmatch(r"[A-Za-z0-9-]+", CSRF_HEADER)

    @pytest.mark.asyncio
    async def test_reads_never_need_a_token(self, app_with_db, pending_story, db_session):
        """Every surviving read-only curation route is reachable with credentials."""
        await db_session.commit()

        client = TestClient(app_with_db)
        for path in ("/", f"/story/{pending_story.id}",
                     f"/api/stories/{pending_story.id}/viewpoints"):
            response = client.get(path, auth=AUTH)
            assert response.status_code == 200, f"{path} returned {response.status_code}"
