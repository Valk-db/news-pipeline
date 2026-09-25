"""Tests for P2: CuratedPost media_urls and snippet integration."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient

from src.schema.models import (
    Story, RawArticle, ReportingUnit, StoryUnitLink,
    MediaAsset, Snippet, CuratedPost, SourceTier
)
from src.shared.config import get_settings
from src.shared import database as database_module
from src.shared.llm import LLMClient


@pytest.fixture
def test_settings(monkeypatch):
    """Configure test settings."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("CURATION_USER", "testuser")
    monkeypatch.setenv("CURATION_PASSWORD", "testpass")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app_with_db(test_settings, db_engine):
    """Create app with test database."""
    # Reset database module globals
    database_module._engine = db_engine
    database_module._async_session_maker = None

    import curation_ui.main as main_module
    from curation_ui.main import app

    main_module.settings = test_settings

    # Replace the global LLMClient with a mock
    import src.shared.llm as llm_module
    llm_module._llm_client = None

    return app


@pytest.fixture
def mock_llm_client():
    """Create a mock LLMClient."""
    mock_llm = AsyncMock(spec=LLMClient)
    mock_llm.generate_caption = AsyncMock(return_value="Test caption with key facts")
    return mock_llm


@pytest.fixture
async def story_with_media_and_snippets(db_session):
    """Create a story with media assets and snippets."""
    # Create story
    story = Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        primary_entities={"PERSON": ["John Doe"], "ORG": ["Acme Corp"]},
        tier1_unit_count=1,
        status=Story.Status.PENDING,
    )
    db_session.add(story)

    # Create reporting unit
    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
    )
    db_session.add(unit)

    # Create raw article
    article = RawArticle(
        id=uuid.uuid4(),
        url="https://apnews.com/article/test-1",
        url_hash="abc123",
        title="Test Article About Politics",
        body_text="This is a test article about politics and government.",
        summary="Test summary",
        source_domain="apnews.com",
        source_tier=SourceTier.TIER1,
        published_at=datetime.now(timezone.utc),
        entities={"PERSON": ["John Doe"], "ORG": ["Acme Corp"], "GPE": ["United States"]},
    )
    db_session.add(article)

    # Update unit's representative article
    unit.representative_article_id = article.id

    # Link story to unit
    link = StoryUnitLink(story_id=story.id, unit_id=unit.id)
    db_session.add(link)

    # Add media assets
    image_asset = MediaAsset(
        story_id=story.id,
        article_id=article.id,
        media_type=MediaAsset.MediaType.IMAGE,
        url="https://example.com/image.jpg",
        thumbnail_url="https://example.com/thumb.jpg",
        alt_text="Test image",
        source="article",
    )
    db_session.add(image_asset)

    video_asset = MediaAsset(
        story_id=story.id,
        article_id=None,
        media_type=MediaAsset.MediaType.VIDEO,
        url="https://www.youtube.com/embed/dQw4w9WgXcQ",
        thumbnail_url="https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        alt_text="Test YouTube Video",
        source="youtube",
        source_id="dQw4w9WgXcQ",
    )
    db_session.add(video_asset)

    # Add snippets
    snippet1 = Snippet(
        story_id=story.id,
        article_id=article.id,
        snippet_type=Snippet.SnippetType.QUOTE,
        text="This is a key quote from the article about the event.",
        confidence=90,
    )
    db_session.add(snippet1)

    snippet2 = Snippet(
        story_id=story.id,
        article_id=article.id,
        snippet_type=Snippet.SnippetType.STAT,
        text="The event affected over 1 million people according to officials.",
        confidence=85,
    )
    db_session.add(snippet2)

    await db_session.commit()
    await db_session.refresh(story)

    return story, article


@pytest.mark.asyncio
async def test_approve_story_populates_media_urls(app_with_db, db_session, story_with_media_and_snippets, mock_llm_client):
    """Test that approve_story populates CuratedPost.media_urls from MediaAsset rows."""
    story, article = story_with_media_and_snippets

    # Set the global mock LLM client
    import src.shared.llm as llm_module
    llm_module._llm_client = mock_llm_client

    client = TestClient(app_with_db)
    response = client.post(f"/story/{story.id}/approve", auth=("testuser", "testpass"))

    assert response.status_code == 200

    # Verify CuratedPost was created with media_urls
    from sqlalchemy import select
    stmt = select(CuratedPost).where(CuratedPost.story_id == story.id)
    result = await db_session.execute(stmt)
    post = result.scalar_one_or_none()

    assert post is not None
    assert post.media_urls is not None
    assert len(post.media_urls) == 2  # 1 image + 1 video

    # Check image entry
    image_entry = next((m for m in post.media_urls if m["type"] == "image"), None)
    assert image_entry is not None
    assert image_entry["url"] == "https://example.com/image.jpg"
    assert image_entry["alt"] == "Test image"

    # Check video entry
    video_entry = next((m for m in post.media_urls if m["type"] == "video"), None)
    assert video_entry is not None
    assert video_entry["url"] == "https://www.youtube.com/embed/dQw4w9WgXcQ"
    assert video_entry["alt"] == "Test YouTube Video"


@pytest.mark.asyncio
async def test_approve_story_feeds_snippets_into_caption(app_with_db, db_session, story_with_media_and_snippets, mock_llm_client):
    """Test that approve_story passes snippet texts to the LLM as key_facts."""
    story, article = story_with_media_and_snippets

    # Set the global mock LLM client
    import src.shared.llm as llm_module
    llm_module._llm_client = mock_llm_client

    client = TestClient(app_with_db)
    response = client.post(f"/story/{story.id}/approve", auth=("testuser", "testpass"))

    assert response.status_code == 200

    # Verify LLM was called with snippet texts in key_facts
    mock_llm_client.generate_caption.assert_called_once()
    call_args = mock_llm_client.generate_caption.call_args
    key_facts = call_args.kwargs.get("key_facts", [])

    # Should include article titles AND snippet texts
    assert len(key_facts) >= 3  # At least 1 title + 2 snippets
    assert "Test Article About Politics" in key_facts
    assert "This is a key quote from the article about the event." in key_facts
    assert "The event affected over 1 million people according to officials." in key_facts


@pytest.mark.asyncio
async def test_approve_story_no_media_when_none_exists(app_with_db, db_session, mock_llm_client):
    """Test that approve_story works when no MediaAsset rows exist."""
    # Create story without media
    story = Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        primary_entities={"PERSON": ["Jane Smith"]},
        tier1_unit_count=1,
        status=Story.Status.PENDING,
    )
    db_session.add(story)

    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
    )
    db_session.add(unit)

    article = RawArticle(
        id=uuid.uuid4(),
        url="https://reuters.com/article/test-2",
        url_hash="def456",
        title="Another Test Article",
        body_text="Content here.",
        source_domain="reuters.com",
        source_tier=SourceTier.TIER1,
        published_at=datetime.now(timezone.utc),
    )
    db_session.add(article)
    unit.representative_article_id = article.id

    link = StoryUnitLink(story_id=story.id, unit_id=unit.id)
    db_session.add(link)

    await db_session.commit()
    await db_session.refresh(story)

    # Set the global mock LLM client
    import src.shared.llm as llm_module
    llm_module._llm_client = mock_llm_client

    client = TestClient(app_with_db)
    response = client.post(f"/story/{story.id}/approve", auth=("testuser", "testpass"))

    assert response.status_code == 200

    # Verify CuratedPost was created with media_urls = None
    from sqlalchemy import select
    stmt = select(CuratedPost).where(CuratedPost.story_id == story.id)
    result = await db_session.execute(stmt)
    post = result.scalar_one_or_none()

    assert post is not None
    assert post.media_urls is None or post.media_urls == []


@pytest.mark.asyncio
async def test_approve_story_caps_media_urls_at_three(app_with_db, db_session, mock_llm_client):
    """Test that media_urls is capped at 3 entries."""
    story = Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        primary_entities={"PERSON": ["Test Person"]},
        tier1_unit_count=1,
        status=Story.Status.PENDING,
    )
    db_session.add(story)

    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
    )
    db_session.add(unit)

    article = RawArticle(
        id=uuid.uuid4(),
        url="https://example.com/article",
        url_hash="ghi789",
        title="Test Article",
        body_text="Content.",
        source_domain="example.com",
        source_tier=SourceTier.TIER1,
        published_at=datetime.now(timezone.utc),
    )
    db_session.add(article)
    unit.representative_article_id = article.id

    link = StoryUnitLink(story_id=story.id, unit_id=unit.id)
    db_session.add(link)

    # Add 5 media assets (more than the cap of 3)
    for i in range(5):
        asset = MediaAsset(
            story_id=story.id,
            article_id=article.id,
            media_type=MediaAsset.MediaType.IMAGE,
            url=f"https://example.com/image{i}.jpg",
            alt_text=f"Image {i}",
            source="article",
        )
        db_session.add(asset)

    await db_session.commit()
    await db_session.refresh(story)

    # Set the global mock LLM client
    import src.shared.llm as llm_module
    llm_module._llm_client = mock_llm_client

    client = TestClient(app_with_db)
    response = client.post(f"/story/{story.id}/approve", auth=("testuser", "testpass"))

    assert response.status_code == 200

    # Verify media_urls is capped at 3
    from sqlalchemy import select
    stmt = select(CuratedPost).where(CuratedPost.story_id == story.id)
    result = await db_session.execute(stmt)
    post = result.scalar_one_or_none()

    assert post is not None
    assert post.media_urls is not None
    assert len(post.media_urls) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])