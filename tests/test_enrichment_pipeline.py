"""Tests for enrichment pipeline - database persistence of enrichment results."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from src.enrichment.pipeline import enrich_story
from src.schema.models import (
    Story, RawArticle, ReportingUnit, StoryUnitLink,
    MediaAsset, StoryEmbedding, SourceTier
)


@pytest.fixture
async def story_with_articles(db_session):
    """Create a story with articles for testing."""
    # Create a story
    story = Story(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        primary_entities={"PERSON": ["John Doe"], "ORG": ["Acme Corp"]},
        tier1_unit_count=1,
        status=Story.Status.PENDING,
    )
    db_session.add(story)

    # Create a reporting unit
    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
        representative_article_id=uuid.uuid4(),  # Will update after article created
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
        body_text="This is a test article about politics and government. It has plenty of content for embeddings and snippet extraction.",
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

    await db_session.commit()
    await db_session.refresh(story)

    return story, article


@pytest.mark.asyncio
async def test_enrich_story_persists_media_assets(db_session, story_with_articles):
    """Test that enrich_story persists MediaAsset rows for article media extraction."""
    story, article = story_with_articles

    # Mock the network-calling functions
    mock_media_result = {
        "images": [
            {"url": "https://example.com/img1.jpg", "alt_text": "Test image 1", "width": 800, "height": 600, "source": "article"},
            {"url": "https://example.com/img2.jpg", "alt_text": "Test image 2", "width": 1200, "height": 800, "source": "article"},
        ],
        "videos": [
            {"url": "https://example.com/video.mp4", "thumbnail_url": "https://example.com/thumb.jpg", "source": "article", "duration_seconds": 120},
        ],
        "embeds": [
            {"url": "https://www.youtube.com/embed/dQw4w9WgXcQ", "type": "youtube", "source": "iframe"},
        ],
        "audio": [
            {"url": "https://example.com/audio.mp3", "source": "article", "duration_seconds": 300},
        ],
    }

    with patch("src.enrichment.pipeline.extract_media_from_article", new_callable=AsyncMock) as mock_media:
        mock_media.return_value = mock_media_result

        with patch("src.enrichment.pipeline.find_videos_for_story", new_callable=AsyncMock) as mock_videos:
            mock_videos.return_value = []

            with patch("src.enrichment.pipeline.find_snippets_for_story", new_callable=AsyncMock) as mock_social:
                mock_social.return_value = []

                with patch("src.enrichment.pipeline.enrich_story_with_snippets", new_callable=AsyncMock) as mock_snippets:
                    mock_snippets.return_value = 0

                    with patch("src.enrichment.pipeline.embed_story", new_callable=AsyncMock) as mock_embeddings:
                        mock_embeddings.return_value = None

                        result = await enrich_story(db_session, str(story.id))

    # Assert results
    assert result["media_assets"] == 5  # 2 images + 1 video + 1 embed + 1 audio
    assert "media" not in result.get("errors", [])

    # Verify MediaAsset rows were created
    from sqlalchemy import select
    stmt = select(MediaAsset).where(MediaAsset.story_id == story.id)
    result = await db_session.execute(stmt)
    media_assets = result.scalars().all()

    assert len(media_assets) == 5

    # Check types
    types = {m.media_type for m in media_assets}
    assert MediaAsset.MediaType.IMAGE in types
    assert MediaAsset.MediaType.VIDEO in types
    assert MediaAsset.MediaType.EMBED in types
    assert MediaAsset.MediaType.AUDIO in types

    # Check article_id is set for article-sourced media
    for asset in media_assets:
        if asset.media_type in {MediaAsset.MediaType.IMAGE, MediaAsset.MediaType.VIDEO, MediaAsset.MediaType.AUDIO}:
            assert asset.article_id == article.id
        if asset.media_type == MediaAsset.MediaType.EMBED:
            assert asset.article_id == article.id


@pytest.mark.asyncio
async def test_enrich_story_persists_video_assets(db_session, story_with_articles):
    """Test that enrich_story persists MediaAsset rows for YouTube/Vimeo videos."""
    story, article = story_with_articles

    mock_video_results = [
        {
            "video_id": "dQw4w9WgXcQ",
            "title": "Test YouTube Video",
            "description": "A test video",
            "thumbnail_url": "https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
            "channel_title": "Test Channel",
            "channel_id": "UC123",
            "published_at": "2024-01-15T10:00:00Z",
            "duration_seconds": 240,
            "view_count": 1000000,
            "like_count": 50000,
            "comment_count": 1000,
            "embed_url": "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "watch_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "source": "youtube",
        },
        {
            "video_id": "123456789",
            "title": "Test Vimeo Video",
            "description": "Another test video",
            "thumbnail_url": "https://vimeo.com/123456789/thumb.jpg",
            "published_at": "2024-01-15T11:00:00Z",
            "duration_seconds": 180,
            "view_count": 50000,
            "embed_url": "https://player.vimeo.com/video/123456789",
            "watch_url": "https://vimeo.com/123456789",
            "source": "vimeo",
        },
    ]

    with patch("src.enrichment.pipeline.extract_media_from_article", new_callable=AsyncMock) as mock_media:
        mock_media.return_value = {"images": [], "videos": [], "embeds": [], "audio": []}

        with patch("src.enrichment.pipeline.find_videos_for_story", new_callable=AsyncMock) as mock_videos:
            mock_videos.return_value = mock_video_results

            with patch("src.enrichment.pipeline.find_snippets_for_story", new_callable=AsyncMock) as mock_social:
                mock_social.return_value = []

                with patch("src.enrichment.pipeline.enrich_story_with_snippets", new_callable=AsyncMock) as mock_snippets:
                    mock_snippets.return_value = 0

                    with patch("src.enrichment.pipeline.embed_story", new_callable=AsyncMock) as mock_embeddings:
                        mock_embeddings.return_value = None

                        result = await enrich_story(db_session, str(story.id))

    assert result["videos_found"] == 2
    assert "videos" not in result.get("errors", [])

    # Verify MediaAsset rows
    from sqlalchemy import select
    stmt = select(MediaAsset).where(MediaAsset.story_id == story.id)
    result = await db_session.execute(stmt)
    media_assets = result.scalars().all()

    assert len(media_assets) == 2
    for asset in media_assets:
        assert asset.media_type == MediaAsset.MediaType.VIDEO
        assert asset.article_id is None
        assert asset.source in {"youtube", "vimeo"}
        assert asset.source_id in {"dQw4w9WgXcQ", "123456789"}
        assert asset.thumbnail_url is not None
        assert asset.duration_seconds is not None


@pytest.mark.asyncio
async def test_enrich_story_persists_social_snippets(db_session, story_with_articles):
    """Test that enrich_story persists MediaAsset rows for social media snippets."""
    story, article = story_with_articles

    mock_social_results = [
        {
            "post_id": "1234567890",
            "text": "This is an important tweet about the story!",
            "author_username": "reporter1",
            "author_verified": True,
            "created_at": "2024-01-15T10:00:00Z",
            "retweet_count": 100,
            "like_count": 500,
            "reply_count": 50,
            "quote_count": 25,
            "url": "https://twitter.com/reporter1/status/1234567890",
            "source": "twitter",
        },
        {
            "post_id": "abc123def",
            "text": "Key insight on Bluesky about this event.",
            "author_handle": "analyst.bsky.social",
            "author_did": "did:plc:abc123",
            "author_avatar": "https://bsky.app/avatar.jpg",
            "created_at": "2024-01-15T11:00:00Z",
            "like_count": 75,
            "repost_count": 20,
            "reply_count": 10,
            "url": "https://bsky.app/profile/analyst.bsky.social/post/abc123def",
            "source": "bluesky",
        },
        {
            "post_id": "reddit123",
            "title": "Reddit discussion about the story",
            "text": "This is a self-post with analysis.",
            "author": "reddit_user",
            "subreddit": "news",
            "score": 500,
            "upvote_ratio": 0.95,
            "num_comments": 100,
            "created_at": "2024-01-15T12:00:00Z",
            "url": "https://reddit.com/r/news/comments/reddit123",
            "source": "reddit",
        },
    ]

    with patch("src.enrichment.pipeline.extract_media_from_article", new_callable=AsyncMock) as mock_media:
        mock_media.return_value = {"images": [], "videos": [], "embeds": [], "audio": []}

        with patch("src.enrichment.pipeline.find_videos_for_story", new_callable=AsyncMock) as mock_videos:
            mock_videos.return_value = []

            with patch("src.enrichment.pipeline.find_snippets_for_story", new_callable=AsyncMock) as mock_social:
                mock_social.return_value = mock_social_results

                with patch("src.enrichment.pipeline.enrich_story_with_snippets", new_callable=AsyncMock) as mock_snippets:
                    mock_snippets.return_value = 0

                    with patch("src.enrichment.pipeline.embed_story", new_callable=AsyncMock) as mock_embeddings:
                        mock_embeddings.return_value = None

                        result = await enrich_story(db_session, str(story.id))

    assert result["social_snippets_found"] == 3
    assert "social_snippets" not in result.get("errors", [])

    # Verify MediaAsset rows
    from sqlalchemy import select
    stmt = select(MediaAsset).where(MediaAsset.story_id == story.id)
    result = await db_session.execute(stmt)
    media_assets = result.scalars().all()

    assert len(media_assets) == 3
    for asset in media_assets:
        assert asset.media_type == MediaAsset.MediaType.EMBED
        assert asset.article_id is None
        assert asset.source in {"twitter", "bluesky", "reddit"}
        assert asset.url is not None
        assert asset.alt_text is not None


@pytest.mark.asyncio
async def test_enrich_story_persists_story_embedding(db_session, story_with_articles):
    """Test that enrich_story persists StoryEmbedding row."""
    story, article = story_with_articles

    mock_embedding_result = {
        "story_id": str(story.id),
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "embedding": [0.1] * 384,
        "dimensions": 384,
    }

    with patch("src.enrichment.pipeline.extract_media_from_article", new_callable=AsyncMock) as mock_media:
        mock_media.return_value = {"images": [], "videos": [], "embeds": [], "audio": []}

        with patch("src.enrichment.pipeline.find_videos_for_story", new_callable=AsyncMock) as mock_videos:
            mock_videos.return_value = []

            with patch("src.enrichment.pipeline.find_snippets_for_story", new_callable=AsyncMock) as mock_social:
                mock_social.return_value = []

                with patch("src.enrichment.pipeline.enrich_story_with_snippets", new_callable=AsyncMock) as mock_snippets:
                    mock_snippets.return_value = 0

                    with patch("src.enrichment.pipeline.embed_story", new_callable=AsyncMock) as mock_embeddings:
                        mock_embeddings.return_value = mock_embedding_result

                        result = await enrich_story(db_session, str(story.id), enable_embeddings=True)

    assert result["embeddings_generated"] == 1
    assert "embeddings" not in result.get("errors", [])

    # Verify StoryEmbedding row
    from sqlalchemy import select
    stmt = select(StoryEmbedding).where(StoryEmbedding.story_id == story.id)
    result = await db_session.execute(stmt)
    embeddings = result.scalars().all()

    assert len(embeddings) == 1
    emb = embeddings[0]
    assert emb.story_id == story.id
    assert emb.model == "sentence-transformers/all-MiniLM-L6-v2"
    assert len(emb.embedding) == 384
    assert emb.dimensions == 384


@pytest.mark.asyncio
async def test_enrich_story_all_enrichments_combined(db_session, story_with_articles):
    """Test that all enrichment types work together and commit atomically."""
    story, article = story_with_articles

    mock_media_result = {
        "images": [{"url": "https://example.com/img1.jpg", "alt_text": "Image 1", "width": 800, "height": 600, "source": "article"}],
        "videos": [],
        "embeds": [],
        "audio": [],
    }

    mock_video_results = [
        {
            "video_id": "vid123",
            "title": "Video Title",
            "description": "Desc",
            "thumbnail_url": "https://example.com/thumb.jpg",
            "published_at": "2024-01-15T10:00:00Z",
            "duration_seconds": 120,
            "embed_url": "https://www.youtube.com/embed/vid123",
            "watch_url": "https://www.youtube.com/watch?v=vid123",
            "source": "youtube",
        },
    ]

    mock_social_results = [
        {
            "post_id": "post123",
            "text": "Social post text",
            "author_username": "user1",
            "created_at": "2024-01-15T10:00:00Z",
            "like_count": 10,
            "url": "https://twitter.com/user1/status/post123",
            "source": "twitter",
        },
    ]

    mock_embedding_result = {
        "story_id": str(story.id),
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "embedding": [0.1] * 384,
        "dimensions": 384,
    }

    with patch("src.enrichment.pipeline.extract_media_from_article", new_callable=AsyncMock) as mock_media:
        mock_media.return_value = mock_media_result

        with patch("src.enrichment.pipeline.find_videos_for_story", new_callable=AsyncMock) as mock_videos:
            mock_videos.return_value = mock_video_results

            with patch("src.enrichment.pipeline.find_snippets_for_story", new_callable=AsyncMock) as mock_social:
                mock_social.return_value = mock_social_results

                with patch("src.enrichment.pipeline.enrich_story_with_snippets", new_callable=AsyncMock) as mock_snippets:
                    mock_snippets.return_value = 0

                    with patch("src.enrichment.pipeline.embed_story", new_callable=AsyncMock) as mock_embeddings:
                        mock_embeddings.return_value = mock_embedding_result

                        result = await enrich_story(db_session, str(story.id))

    # Check all counts
    assert result["media_assets"] == 1
    assert result["videos_found"] == 1
    assert result["social_snippets_found"] == 1
    assert result["embeddings_generated"] == 1

    # Verify all rows exist
    from sqlalchemy import select
    stmt = select(MediaAsset).where(MediaAsset.story_id == story.id)
    result = await db_session.execute(stmt)
    media_assets = result.scalars().all()
    assert len(media_assets) == 3

    stmt = select(StoryEmbedding).where(StoryEmbedding.story_id == story.id)
    result = await db_session.execute(stmt)
    embeddings = result.scalars().all()
    assert len(embeddings) == 1


@pytest.mark.asyncio
async def test_enrich_story_counts_match_rows(db_session, story_with_articles):
    """Test that results['media_assets'] and results['videos_found'] match actual DB row counts."""
    story, article = story_with_articles

    mock_media_result = {
        "images": [
            {"url": "https://example.com/img1.jpg", "source": "article"},
            {"url": "https://example.com/img2.jpg", "source": "article"},
            {"url": "https://example.com/img3.jpg", "source": "article"},
        ],
        "videos": [],
        "embeds": [],
        "audio": [],
    }

    mock_video_results = [
        {"video_id": "v1", "title": "V1", "embed_url": "https://youtube.com/embed/v1", "source": "youtube"},
        {"video_id": "v2", "title": "V2", "embed_url": "https://youtube.com/embed/v2", "source": "youtube"},
    ]

    with patch("src.enrichment.pipeline.extract_media_from_article", new_callable=AsyncMock) as mock_media:
        mock_media.return_value = mock_media_result

        with patch("src.enrichment.pipeline.find_videos_for_story", new_callable=AsyncMock) as mock_videos:
            mock_videos.return_value = mock_video_results

            with patch("src.enrichment.pipeline.find_snippets_for_story", new_callable=AsyncMock) as mock_social:
                mock_social.return_value = []

                with patch("src.enrichment.pipeline.enrich_story_with_snippets", new_callable=AsyncMock) as mock_snippets:
                    mock_snippets.return_value = 0

                    with patch("src.enrichment.pipeline.embed_story", new_callable=AsyncMock) as mock_embeddings:
                        mock_embeddings.return_value = None

                        result = await enrich_story(db_session, str(story.id))

    # Verify counts match actual rows
    from sqlalchemy import select
    stmt = select(MediaAsset).where(MediaAsset.story_id == story.id)
    query_result = await db_session.execute(stmt)
    media_assets = query_result.scalars().all()

    assert result["media_assets"] == 3  # 3 images from article
    assert result["videos_found"] == 2  # 2 videos from API
    assert len(media_assets) == 5  # 3 + 2


@pytest.mark.asyncio
async def test_enrich_story_handles_partial_failures(db_session, story_with_articles):
    """Test that one failed task doesn't prevent others from persisting."""
    story, article = story_with_articles

    mock_media_result = {
        "images": [{"url": "https://example.com/img1.jpg", "source": "article"}],
        "videos": [], "embeds": [], "audio": [],
    }

    with patch("src.enrichment.pipeline.extract_media_from_article", new_callable=AsyncMock) as mock_media:
        mock_media.return_value = mock_media_result

        with patch("src.enrichment.pipeline.find_videos_for_story", new_callable=AsyncMock) as mock_videos:
            # This one fails
            mock_videos.side_effect = Exception("YouTube API quota exceeded")

            with patch("src.enrichment.pipeline.find_snippets_for_story", new_callable=AsyncMock) as mock_social:
                mock_social.return_value = []

                with patch("src.enrichment.pipeline.enrich_story_with_snippets", new_callable=AsyncMock) as mock_snippets:
                    mock_snippets.return_value = 0

                    with patch("src.enrichment.pipeline.embed_story", new_callable=AsyncMock) as mock_embeddings:
                        mock_embeddings.return_value = None

                        result = await enrich_story(db_session, str(story.id))

    # Media should still be persisted despite video failure
    assert result["media_assets"] == 1
    assert "videos" in str(result["errors"])

    from sqlalchemy import select
    stmt = select(MediaAsset).where(MediaAsset.story_id == story.id)
    result = await db_session.execute(stmt)
    media_assets = result.scalars().all()
    assert len(media_assets) == 1


@pytest.mark.asyncio
async def test_enrich_story_handles_commit_failure(db_session, story_with_articles):
    """Test that DB commit failure is handled gracefully."""
    story, article = story_with_articles

    mock_media_result = {
        "images": [{"url": "https://example.com/img1.jpg", "source": "article"}],
        "videos": [], "embeds": [], "audio": [],
    }

    with patch("src.enrichment.pipeline.extract_media_from_article", new_callable=AsyncMock) as mock_media:
        mock_media.return_value = mock_media_result

        with patch("src.enrichment.pipeline.find_videos_for_story", new_callable=AsyncMock) as mock_videos:
            mock_videos.return_value = []

            with patch("src.enrichment.pipeline.find_snippets_for_story", new_callable=AsyncMock) as mock_social:
                mock_social.return_value = []

                with patch("src.enrichment.pipeline.enrich_story_with_snippets", new_callable=AsyncMock) as mock_snippets:
                    mock_snippets.return_value = 0

                    with patch("src.enrichment.pipeline.embed_story", new_callable=AsyncMock) as mock_embeddings:
                        mock_embeddings.return_value = None

                        # Make commit fail
                        db_session.commit = AsyncMock(side_effect=Exception("DB connection lost"))

                        result = await enrich_story(db_session, str(story.id))

    # Should record error but not crash
    assert "commit" in str(result["errors"])