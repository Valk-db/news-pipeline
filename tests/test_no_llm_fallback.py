"""Tests for curation with no LLM configured, and for the configured (AI assisted) path.

The unconfigured path must never touch an LLM: edit renders a blank draft with
a notice, approve builds a deterministic caption, and save uses the caption from
the form.
"""

import html as html_lib
import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.schema.models import (
    CuratedPost,
    RawArticle,
    ReportingUnit,
    SourceTier,
    Story,
    StoryUnitLink,
)
from src.shared import database as database_module
from src.shared.config import get_settings
from src.shared.llm import PLATFORM_LIMITS, build_deterministic_caption

AUTH = ("testuser", "testpass")


@pytest.fixture
def test_settings(monkeypatch):
    """Configure test settings with no provider key."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app_with_db(test_settings, db_engine):
    """Create app with test database and no injected LLM client."""
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    from curation_ui.main import app

    main_module.settings = test_settings

    import src.shared.llm as llm_module
    llm_module._llm_client = None

    return app


@pytest.fixture
def forbid_llm(monkeypatch):
    """Fail the test if any LLM client is created or asked for a caption."""

    async def _no_client(*args, **kwargs):
        raise AssertionError("get_llm_client must not be called with no LLM configured")

    async def _no_caption(*args, **kwargs):
        raise AssertionError("generate_caption must not be called with no LLM configured")

    import curation_ui.main as main_module
    import src.shared.llm as llm_module

    monkeypatch.setattr(llm_module, "_llm_client", None)
    monkeypatch.setattr(llm_module, "get_llm_client", _no_client)
    monkeypatch.setattr(llm_module.LLMClient, "generate_caption", _no_caption)
    monkeypatch.setattr(main_module, "get_llm_client", _no_client)
    return llm_module


class StubLLMClient:
    """Minimal LLM client stand-in, injected the way check_llm_available expects."""

    def __init__(self, caption: str):
        self.caption = caption
        self.calls = []

    async def generate_caption(self, **kwargs):
        self.calls.append(kwargs)
        return self.caption


@pytest.fixture
async def pending_story(db_session):
    """Create a PENDING story backed by one reporting unit and one article."""
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    story = Story(
        id=uuid.uuid4(),
        day=day,
        primary_entities=["test-entity"],
        status=Story.Status.PENDING,
        tier1_unit_count=2,
        distinct_owners=2,
    )
    db_session.add(story)

    article = RawArticle(
        id=uuid.uuid4(),
        url="https://apnews.com/article/test-1",
        url_hash="abc123",
        title="Test Article About Politics",
        body_text="This is the body of the test article about politics and government.",
        source_domain="apnews.com",
        source_tier=SourceTier.TIER1,
        published_at=datetime.now(timezone.utc),
    )
    db_session.add(article)

    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=day,
        representative_article_id=article.id,
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
    )
    db_session.add(unit)
    db_session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))

    await db_session.commit()
    await db_session.refresh(story)

    return story, article


def textarea_caption(html: str) -> str:
    """Extract the caption textarea contents from a rendered edit page."""
    match = re.search(r'<textarea name="caption"[^>]*>(.*?)</textarea>', html, re.DOTALL)
    assert match, "caption textarea not found in response"
    return html_lib.unescape(match.group(1)).strip()


class TestBuildDeterministicCaption:
    """The no-LLM caption builder must be pure and fit the platform limit."""

    def test_same_inputs_give_same_output(self):
        args = {
            "story_title": "Test Article About Politics",
            "key_facts": ["Test Article About Politics", "Another fact"],
            "source_urls": ["https://apnews.com/article/test-1"],
            "platform": "twitter",
        }
        assert build_deterministic_caption(**args) == build_deterministic_caption(**args)

    def test_uses_first_key_fact_and_source_url(self):
        caption = build_deterministic_caption(
            story_title="Story title",
            key_facts=["First fact about the event", "Second fact"],
            source_urls=["https://apnews.com/article/test-1", "https://reuters.com/article/test-2"],
            platform="twitter",
        )
        assert caption == "First fact about the event https://apnews.com/article/test-1"

    def test_falls_back_to_story_title(self):
        caption = build_deterministic_caption(
            story_title="Only the story title",
            key_facts=[],
            source_urls=[],
            platform="twitter",
        )
        assert caption == "Only the story title"

    def test_trims_to_platform_limit_with_long_text_and_url(self):
        url = "https://example.com/a-very-long-source-url-for-a-news-article"
        caption = build_deterministic_caption(
            story_title="x" * 500,
            key_facts=["y" * 500],
            source_urls=[url],
            platform="twitter",
        )
        assert len(caption) <= PLATFORM_LIMITS["twitter"]
        assert caption.endswith(url)

    def test_result_passes_caption_validation(self):
        from src.shared.llm import validate_caption

        caption = build_deterministic_caption(
            story_title="Test Article About Politics",
            key_facts=["Test Article About Politics"],
            source_urls=["https://apnews.com/article/test-1"],
            platform="twitter",
        )
        is_valid, error = validate_caption(caption, "twitter", [])
        assert is_valid, error


class TestNoLLMConfigured:
    """Editing and approving must work end to end with no provider key."""

    @pytest.mark.asyncio
    async def test_edit_page_renders_blank_draft_with_notice(
        self, app_with_db, forbid_llm, pending_story
    ):
        """GET /edit returns 200 with an empty draft and an AI unavailable notice."""
        story, _ = pending_story

        client = TestClient(app_with_db)
        response = client.get(f"/story/{story.id}/edit", auth=AUTH)

        assert response.status_code == 200
        assert textarea_caption(response.text) == ""
        assert "AI assistance is unavailable" in response.text
        assert "Write your caption manually" in response.text
        assert forbid_llm._llm_client is None

    @pytest.mark.asyncio
    async def test_approve_creates_deterministic_caption(
        self, app_with_db, forbid_llm, pending_story, db_session, csrf_headers
    ):
        """POST /approve succeeds with no LLM and stores the deterministic caption."""
        story, article = pending_story
        expected = build_deterministic_caption(
            story_title=article.title,
            key_facts=[article.title],
            source_urls=[article.url],
            platform="twitter",
        )

        client = TestClient(app_with_db)
        response = client.post(f"/story/{story.id}/approve", auth=AUTH, headers=csrf_headers)

        assert response.status_code == 200, response.text[:500]
        assert "AI assistance is unavailable" in response.text
        assert "deterministic draft" in response.text

        result = await db_session.execute(
            select(CuratedPost).where(CuratedPost.story_id == story.id)
        )
        post = result.scalar_one()
        assert post.caption == expected
        assert post.status == CuratedPost.Status.APPROVED
        assert post.source_urls == [article.url]

        result = await db_session.execute(select(Story).where(Story.id == story.id))
        updated_story = result.scalar_one()
        await db_session.refresh(updated_story)
        assert updated_story.status == Story.Status.QUEUED

    @pytest.mark.asyncio
    async def test_save_manual_caption_never_calls_llm(
        self, app_with_db, forbid_llm, pending_story, db_session, csrf_headers
    ):
        """POST /save stores the caption from the form without any LLM call."""
        story, _ = pending_story
        manual_caption = "Officials described the situation in a short statement."

        client = TestClient(app_with_db)
        response = client.post(
            f"/story/{story.id}/save",
            data={"caption": manual_caption, "platform": "twitter"},
            auth=AUTH,
            headers=csrf_headers,
        )

        assert response.status_code == 200, response.text[:500]

        result = await db_session.execute(
            select(CuratedPost).where(CuratedPost.story_id == story.id)
        )
        post = result.scalar_one()
        assert post.caption == manual_caption

        result = await db_session.execute(select(Story).where(Story.id == story.id))
        updated_story = result.scalar_one()
        await db_session.refresh(updated_story)
        assert updated_story.status == Story.Status.QUEUED

    @pytest.mark.asyncio
    async def test_edit_page_prefills_existing_caption(
        self, app_with_db, forbid_llm, pending_story, db_session
    ):
        """GET /edit prefills the caption already saved for the story."""
        story, article = pending_story
        saved_caption = "An earlier draft that the curator already wrote."
        db_session.add(CuratedPost(
            story_id=story.id,
            platform="twitter",
            caption=saved_caption,
            source_urls=[article.url],
            status=CuratedPost.Status.APPROVED,
        ))
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/story/{story.id}/edit", auth=AUTH)

        assert response.status_code == 200
        assert textarea_caption(response.text) == saved_caption
        assert "AI assistance is unavailable" in response.text


class TestLLMConfigured:
    """With a client in place, the AI assisted behavior is unchanged."""

    @pytest.fixture
    def stub_llm(self, monkeypatch):
        """Inject a stub LLM client on src.shared.llm._llm_client."""
        import src.shared.llm as llm_module

        stub = StubLLMClient("AI written caption with source https://apnews.com/article/test-1")
        llm_module._llm_client = stub
        return stub

    @pytest.mark.asyncio
    async def test_edit_page_prefills_ai_draft(self, app_with_db, stub_llm, pending_story):
        """GET /edit shows the LLM draft and notes that AI assistance was used."""
        story, _ = pending_story

        client = TestClient(app_with_db)
        response = client.get(f"/story/{story.id}/edit", auth=AUTH)

        assert response.status_code == 200
        assert textarea_caption(response.text) == stub_llm.caption
        assert "Draft generated with AI assistance." in response.text
        assert "AI assistance is unavailable" not in response.text
        assert len(stub_llm.calls) == 1

    @pytest.mark.asyncio
    async def test_approve_uses_ai_caption(
        self, app_with_db, stub_llm, pending_story, db_session, csrf_headers
    ):
        """POST /approve stores the caption returned by the LLM client."""
        story, _ = pending_story

        client = TestClient(app_with_db)
        response = client.post(f"/story/{story.id}/approve", auth=AUTH, headers=csrf_headers)

        assert response.status_code == 200, response.text[:500]

        result = await db_session.execute(
            select(CuratedPost).where(CuratedPost.story_id == story.id)
        )
        post = result.scalar_one()
        assert post.caption == stub_llm.caption
        assert len(stub_llm.calls) == 1

    @pytest.mark.asyncio
    async def test_edit_page_prefers_saved_caption_over_ai_draft(
        self, app_with_db, stub_llm, pending_story, db_session
    ):
        """A saved caption wins over regenerating a draft with the LLM."""
        story, article = pending_story
        saved_caption = "The curator's own wording for this story."
        db_session.add(CuratedPost(
            story_id=story.id,
            platform="twitter",
            caption=saved_caption,
            source_urls=[article.url],
            status=CuratedPost.Status.APPROVED,
        ))
        await db_session.commit()

        client = TestClient(app_with_db)
        response = client.get(f"/story/{story.id}/edit", auth=AUTH)

        assert response.status_code == 200
        assert textarea_caption(response.text) == saved_caption
        assert stub_llm.calls == []