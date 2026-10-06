"""Tests for P2: media assets and snippets reaching the curation surfaces.

The original version of this file drove the approve flow and asserted
`CuratedPost.media_urls` and the snippet text handed to the LLM as key facts.
That flow is gone (see tests/test_route_table.py for the route table contract), so
these tests now assert the same two pieces of data reaching the surfaces that
replaced it: the lead image on the card, and the media list and snippets on the
read-only detail view. The coverage of "media and snippets actually get loaded
and rendered" is preserved; only the consumer changed.
"""

import re

import pytest
import uuid
from datetime import datetime, UTC
from fastapi.testclient import TestClient

from src.schema.models import (
    Story, RawArticle, ReportingUnit, StoryUnitLink,
    MediaAsset, Snippet, SourceTier
)
from src.shared.config import get_settings
from src.shared import database as database_module

AUTH = ("testuser", "testpass")


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

    import src.shared.llm as llm_module
    llm_module._llm_client = None

    return app


def _today() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


async def _link_story_to_article(db_session, *, title, domain, url, url_hash, body="Content here."):
    """A minimal pending story with exactly one attributed source article."""
    story = Story(
        id=uuid.uuid4(),
        day=_today(),
        primary_entities=["11111111-1111-1111-1111-111111111111"],
        tier1_unit_count=1,
        distinct_owners=1,
        status=Story.Status.PENDING,
    )
    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=_today(),
        representative_article_id=uuid.uuid4(),
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"AP": 1},
        tier1_owner_groups={"AP": 1},
    )
    article = RawArticle(
        id=uuid.uuid4(),
        url=url,
        url_hash=url_hash,
        title=title,
        body_text=body,
        source_domain=domain,
        source_tier=SourceTier.TIER1,
        published_at=datetime.now(UTC),
    )
    unit.representative_article_id = article.id
    db_session.add_all([story, unit, article, StoryUnitLink(story_id=story.id, unit_id=unit.id)])
    await db_session.commit()
    await db_session.refresh(story)
    return story, article


@pytest.fixture
async def story_with_media_and_snippets(db_session):
    """A story carrying one image, one video and two snippets."""
    story, article = await _link_story_to_article(
        db_session,
        title="Test Article About Politics",
        domain="apnews.com",
        url="https://apnews.com/article/test-1",
        url_hash="abc123",
        body="This is a test article about politics and government.",
    )

    db_session.add_all([
        MediaAsset(
            story_id=story.id,
            article_id=article.id,
            media_type=MediaAsset.MediaType.IMAGE,
            url="https://example.com/image.jpg",
            thumbnail_url="https://example.com/thumb.jpg",
            alt_text="Test image",
            source="article",
        ),
        MediaAsset(
            story_id=story.id,
            article_id=None,
            media_type=MediaAsset.MediaType.VIDEO,
            url="https://www.youtube.com/embed/dQw4w9WgXcQ",
            thumbnail_url="https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
            alt_text="Test YouTube Video",
            source="youtube",
            source_id="dQw4w9WgXcQ",
        ),
        Snippet(
            story_id=story.id,
            article_id=article.id,
            snippet_type=Snippet.SnippetType.QUOTE,
            text="This is a key quote from the article about the event.",
            confidence=90,
        ),
        Snippet(
            story_id=story.id,
            article_id=article.id,
            snippet_type=Snippet.SnippetType.STAT,
            text="The event affected over 1 million people according to officials.",
            confidence=85,
        ),
    ])

    await db_session.commit()
    await db_session.refresh(story)
    return story, article


@pytest.mark.asyncio
async def test_card_shows_the_lead_image(app_with_db, db_session, story_with_media_and_snippets):
    """The card's top image is the story's first image asset."""
    story, _ = story_with_media_and_snippets

    html = TestClient(app_with_db).get("/", auth=AUTH).text

    assert "https://example.com/image.jpg" in html
    # Only the image leads the card; the video belongs to the detail page.
    assert "https://www.youtube.com/embed/dQw4w9WgXcQ" not in html


@pytest.mark.asyncio
async def test_detail_view_lists_every_media_type(app_with_db, db_session, story_with_media_and_snippets):
    """The detail view keeps the full media list, one entry per media type."""
    story, _ = story_with_media_and_snippets

    response = TestClient(app_with_db).get(f"/story/{story.id}", auth=AUTH)

    assert response.status_code == 200
    assert "https://example.com/image.jpg" in response.text
    assert "https://www.youtube.com/embed/dQw4w9WgXcQ" in response.text


@pytest.mark.asyncio
async def test_detail_view_shows_snippets(app_with_db, db_session, story_with_media_and_snippets):
    """Snippet text reaches the detail view, inside the technical details section."""
    story, _ = story_with_media_and_snippets

    response = TestClient(app_with_db).get(f"/story/{story.id}", auth=AUTH)

    assert response.status_code == 200
    assert "This is a key quote from the article about the event." in response.text
    assert "The event affected over 1 million people according to officials." in response.text


@pytest.mark.asyncio
async def test_detail_view_renders_with_no_media_at_all(app_with_db, db_session):
    """A story with no MediaAsset rows renders the pages without a media block."""
    story, _ = await _link_story_to_article(
        db_session,
        title="Another Test Article",
        domain="reuters.com",
        url="https://reuters.com/article/test-2",
        url_hash="def456",
    )

    client = TestClient(app_with_db)
    detail = client.get(f"/story/{story.id}", auth=AUTH)
    queue = client.get("/", auth=AUTH)

    assert detail.status_code == 200
    assert queue.status_code == 200
    assert "story-media" not in detail.text
    assert "story-media" not in queue.text


@pytest.mark.asyncio
async def test_media_of_the_same_type_is_deduplicated(app_with_db, db_session):
    """Five renditions of one photo is one photo, not five entries in the strip.

    This is the read-only successor to the old `media_urls is capped at 3` test: the
    cap is still there but it is now expressed as one asset per media type.
    """
    story, article = await _link_story_to_article(
        db_session,
        title="Test Article",
        domain="example.com",
        url="https://example.com/article",
        url_hash="ghi789",
    )
    for index in range(5):
        db_session.add(MediaAsset(
            story_id=story.id,
            article_id=article.id,
            media_type=MediaAsset.MediaType.IMAGE,
            url=f"https://example.com/image{index}.jpg",
            alt_text=f"Image {index}",
            source="article",
        ))
    await db_session.commit()
    await db_session.refresh(story)

    detail = TestClient(app_with_db).get(f"/story/{story.id}", auth=AUTH).text

    # Exactly one rendition survives the dedup, and it appears twice because the
    # detail page shows the lead image at the top and then lists it with the rest.
    survivors = set(re.findall(r"https://example\.com/image\d\.jpg", detail))
    assert len(survivors) == 1, f"expected one rendition, got {survivors}"
    assert detail.count('class="detail-media-item"') == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
