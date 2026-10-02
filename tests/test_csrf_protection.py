"""CSRF: the four mutating routes only accept requests that carry the app's token.

HTTP Basic credentials are cached by the browser and re-attached to cross-origin
requests, so without this an attacker's page can POST to the triage routes as the
curator. htmx sends the token minted with the page, in the X-CSRF-Token header.
"""

import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.schema.models import CuratedPost, RawArticle, ReportingUnit, SourceTier, Story, StoryUnitLink
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
    """One PENDING story with a linked unit and article, ready to triage."""
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


def _another_story(db_session, like):
    """Another PENDING story: approve, reject and save each move one out of PENDING."""
    other = Story(
        id=uuid.uuid4(),
        day=like.day,
        primary_entities=["other-entity"],
        status=Story.Status.PENDING,
        tier1_unit_count=2,
        distinct_owners=2,
    )
    db_session.add(other)
    return other


def _approved_post(db_session, story):
    post = CuratedPost(
        id=uuid.uuid4(),
        story_id=story.id,
        platform="twitter",
        caption="Test caption",
        source_urls=[],
        status=CuratedPost.Status.APPROVED,
    )
    db_session.add(post)
    return post


def _csrf_headers():
    from curation_ui.security import CSRF_HEADER, issue_csrf_token
    return {CSRF_HEADER: issue_csrf_token()}


def _mutating_cases(db_session, like):
    """The four state-changing routes, each on a story of its own, plus the status
    a token-carrying request gets. Approve, reject and save all move the story out
    of PENDING, and mark-posted needs a curated post that already exists."""
    approve = _another_story(db_session, like)
    reject = _another_story(db_session, like)
    save = _another_story(db_session, like)
    post = _approved_post(db_session, _another_story(db_session, like))
    return [
        (f"/story/{approve.id}/approve", None, 200),
        (f"/story/{reject.id}/reject", None, 200),
        (f"/story/{save.id}/save", {"caption": "A caption"}, 303),
        (f"/post/{post.id}/mark-posted", None, 303),
    ]


class TestTokenIsRequired:
    """A POST with credentials but no token is refused."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("headers", [
        None,
        {},
        {"X-CSRF-Token": ""},
        {"X-CSRF-Token": "not-a-token"},
        {"X-CSRF-Token": "abc.def"},  # right shape, unsigned
    ])
    async def test_post_without_a_valid_token_is_403(self, app_with_db, db_session, pending_story, headers):
        cases = _mutating_cases(db_session, pending_story)
        await db_session.commit()

        client = TestClient(app_with_db)
        for path, data, _expected in cases:
            response = client.post(path, data=data, auth=AUTH, headers=headers)
            assert response.status_code == 403, f"POST {path} returned {response.status_code}"
            assert "CSRF" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_token_for_another_page_is_refused(self, app_with_db, db_session, pending_story):
        """A valid token is still only valid for the credentials that signed it."""
        from curation_ui.security import CSRF_HEADER, issue_csrf_token

        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.post(
            f"/story/{pending_story.id}/approve",
            auth=AUTH,
            headers={CSRF_HEADER: issue_csrf_token() + "x"},  # tampered signature
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_token_signed_with_other_credentials_is_refused(
        self, app_with_db, db_session, pending_story, monkeypatch
    ):
        """Rotating CURATION_PASSWORD invalidates tokens already in a browser tab."""
        from curation_ui.security import CSRF_HEADER, issue_csrf_token

        await db_session.commit()

        stale = {CSRF_HEADER: issue_csrf_token()}
        monkeypatch.setenv("CURATION_PASSWORD", "rotated-password")
        get_settings.cache_clear()
        rotated = ("testuser", "rotated-password")

        client = TestClient(app_with_db)
        assert client.post(
            f"/story/{pending_story.id}/approve", auth=rotated
        ).status_code == 403
        assert client.post(
            f"/story/{pending_story.id}/approve", auth=rotated, headers=stale
        ).status_code == 403

        # A token minted under the new credentials works.
        assert client.post(
            f"/story/{pending_story.id}/approve", auth=rotated, headers=_csrf_headers()
        ).status_code == 200

    def test_unauthenticated_post_is_401_not_403(self, app_with_db):
        """Auth is still checked first, so an anonymous POST leaks nothing."""
        client = TestClient(app_with_db)
        response = client.post(f"/story/{uuid.uuid4()}/approve")
        assert response.status_code == 401
        assert "www-authenticate" in response.headers


class TestTokenWorks:
    """With a token the routes behave exactly as before."""

    @pytest.mark.asyncio
    async def test_all_four_routes_accept_a_valid_token(self, app_with_db, db_session, pending_story):
        cases = _mutating_cases(db_session, pending_story)
        await db_session.commit()

        # Not following redirects: mark-posted answers 303 to /posts.
        client = TestClient(app_with_db, follow_redirects=False)
        for path, data, expected in cases:
            response = client.post(path, data=data, auth=AUTH, headers=_csrf_headers())
            assert response.status_code == expected, f"POST {path}: {response.text[:200]}"

    @pytest.mark.asyncio
    async def test_token_rendered_into_the_page_works(self, app_with_db, db_session, pending_story):
        """The token the triage page hands to htmx is the one the route accepts."""
        client = TestClient(app_with_db)
        page = client.get("/", auth=AUTH)
        assert page.status_code == 200

        token = re.search(r'hx-headers=\'\{"X-CSRF-Token": "([^"]+)"\}\'', page.text).group(1)

        response = client.post(
            f"/story/{pending_story.id}/approve",
            auth=AUTH,
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 200, response.text[:200]

    @pytest.mark.asyncio
    async def test_posts_page_carries_a_token_too(self, app_with_db, db_session, pending_story):
        """Mark-as-posted is on /posts, which needs the token for the same reason."""
        post = _approved_post(db_session, pending_story)
        await db_session.commit()

        client = TestClient(app_with_db)
        page = client.get("/posts", auth=AUTH)
        assert page.status_code == 200
        assert 'hx-headers=\'{"X-CSRF-Token"' in page.text

        response = client.post(
            f"/post/{post.id}/mark-posted",
            auth=AUTH,
            headers={"X-CSRF-Token": "forged"},
        )
        assert response.status_code == 403


class TestTokenProperties:
    """Unit level: what the token is and is not."""

    def test_tokens_are_unique_per_call(self):
        from curation_ui.security import issue_csrf_token

        assert len({issue_csrf_token() for _ in range(20)}) == 20

    def test_public_pages_carry_no_token(self, app_with_db):
        """An anonymous page must not hand out a token."""
        client = TestClient(app_with_db)
        for path in ("/map", "/globe", "/stories/" + str(uuid.uuid4())):
            response = client.get(path)
            assert "csrf_token" not in response.text
            assert "X-CSRF-Token" not in response.text

    @pytest.mark.asyncio
    async def test_reads_never_need_a_token(self, app_with_db, pending_story, db_session):
        """Only the four mutating routes are guarded."""
        await db_session.commit()

        client = TestClient(app_with_db)
        for path in ("/", "/posts", f"/story/{pending_story.id}/edit",
                     f"/api/stories/{pending_story.id}/viewpoints"):
            response = client.get(path, auth=AUTH)
            assert response.status_code == 200, f"{path} returned {response.status_code}"