"""Tests for enrichment functionality."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.schema.models import (
    Story, RawArticle, ReportingUnit, SourceTier,
    Snippet, MediaAsset, ArticleEmbedding, StoryEmbedding
)


def test_enrichment_models_exist():
    """Test that all enrichment models are defined."""
    # Snippet model
    snippet = Snippet(
        id=uuid.uuid4(),
        story_id=uuid.uuid4(),
        article_id=uuid.uuid4(),
        snippet_type=Snippet.SnippetType.QUOTE,
        text="Test quote",
        entities=["entity1"],
        minhash_signature=[1, 2, 3],
        confidence=90,
        position=1000,
    )
    assert snippet.snippet_type == Snippet.SnippetType.QUOTE
    assert snippet.confidence == 90

    # MediaAsset model
    media = MediaAsset(
        id=uuid.uuid4(),
        article_id=uuid.uuid4(),
        story_id=uuid.uuid4(),
        media_type=MediaAsset.MediaType.IMAGE,
        url="https://example.com/image.jpg",
        thumbnail_url="https://example.com/thumb.jpg",
        alt_text="Test image",
        width=800,
        height=600,
        source="article",
        source_id="img1",
        meta_data={"original": True},
    )
    assert media.media_type == MediaAsset.MediaType.IMAGE
    assert media.meta_data == {"original": True}

    # ArticleEmbedding model
    emb = ArticleEmbedding(
        id=uuid.uuid4(),
        article_id=uuid.uuid4(),
        model="test-model",
        embedding=[0.1, 0.2, 0.3],
        dimensions=3,
    )
    assert emb.dimensions == 3

    # StoryEmbedding model
    story_emb = StoryEmbedding(
        id=uuid.uuid4(),
        story_id=uuid.uuid4(),
        model="test-model",
        embedding=[0.1, 0.2, 0.3],
        dimensions=3,
    )
    assert story_emb.dimensions == 3


def test_snippet_types():
    """Test SnippetType enum values."""
    assert Snippet.SnippetType.QUOTE == "quote"
    assert Snippet.SnippetType.STAT == "stat"
    assert Snippet.SnippetType.FACT == "fact"
    assert Snippet.SnippetType.SUMMARY == "summary"
    assert Snippet.SnippetType.CLAIM == "claim"


def test_media_types():
    """Test MediaAsset.MediaType enum values."""
    assert MediaAsset.MediaType.IMAGE == "image"
    assert MediaAsset.MediaType.VIDEO == "video"
    assert MediaAsset.MediaType.AUDIO == "audio"
    assert MediaAsset.MediaType.EMBED == "embed"


def test_media_extractor_imports():
    """Test media extractor functions can be imported."""
    from src.enrichment.media_extractor import (
        extract_media_from_article,
        extract_media_from_html,
        extract_video_embeds,
        extract_social_embeds,
        classify_embed,
        build_video_embed_url,
        build_social_embed_url,
        deduplicate_media,
    )
    assert callable(extract_media_from_article)
    assert callable(extract_media_from_html)
    assert callable(extract_video_embeds)
    assert callable(extract_social_embeds)
    assert callable(classify_embed)
    assert callable(build_video_embed_url)
    assert callable(build_social_embed_url)
    assert callable(deduplicate_media)


def test_video_finder_imports():
    """Test video finder classes and functions can be imported."""
    from src.enrichment.video_finder import (
        YouTubeFinder,
        VimeoFinder,
        find_related_videos,
        find_videos_for_story,
    )
    assert YouTubeFinder is not None
    assert VimeoFinder is not None
    assert callable(find_related_videos)
    assert callable(find_videos_for_story)


def test_social_snippets_imports():
    """Test social snippets classes and functions can be imported."""
    from src.enrichment.social_snippets import (
        SocialSnippetFinder,
        BlueskyFinder,
        RedditFinder,
        find_social_snippets,
        find_snippets_for_story,
    )
    assert SocialSnippetFinder is not None
    assert BlueskyFinder is not None
    assert RedditFinder is not None
    assert callable(find_social_snippets)
    assert callable(find_snippets_for_story)


def test_snippet_extractor_imports():
    """Test snippet extractor functions can be imported."""
    from src.enrichment.snippet_extractor import (
        extract_snippets_from_article,
        extract_snippets_for_story,
        enrich_story_with_snippets,
        extract_key_quotes,
        extract_statistics,
        extract_factual_claims,
        deduplicate_snippets,
    )
    assert callable(extract_snippets_from_article)
    assert callable(extract_snippets_for_story)
    assert callable(enrich_story_with_snippets)
    assert callable(extract_key_quotes)
    assert callable(extract_statistics)
    assert callable(extract_factual_claims)
    assert callable(deduplicate_snippets)


def test_embedding_service_imports():
    """Test embedding service classes and functions can be imported."""
    from src.enrichment.embedding_service import (
        EmbeddingService,
        get_embedding_service,
        embed_article,
        embed_story,
        cluster_embeddings,
        cosine_similarity,
        normalize_embedding,
        average_embeddings,
    )
    assert EmbeddingService is not None
    assert callable(get_embedding_service)
    assert callable(embed_article)
    assert callable(embed_story)
    assert callable(cluster_embeddings)
    assert callable(cosine_similarity)
    assert callable(normalize_embedding)
    assert callable(average_embeddings)


def test_pipeline_imports():
    """Test enrichment pipeline functions can be imported."""
    from src.enrichment.pipeline import (
        enrich_story,
        enrich_stories_batch,
        enrich_recent_stories,
        extract_key_entities,
    )
    assert callable(enrich_story)
    assert callable(enrich_stories_batch)
    assert callable(enrich_recent_stories)
    assert callable(extract_key_entities)


@pytest.mark.asyncio
async def test_cosine_similarity():
    """Test cosine similarity utility."""
    from src.enrichment.embedding_service import cosine_similarity

    # Identical vectors
    v1 = [1.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0]
    assert cosine_similarity(v1, v2) == 1.0

    # Orthogonal vectors
    v1 = [1.0, 0.0, 0.0]
    v2 = [0.0, 1.0, 0.0]
    assert abs(cosine_similarity(v1, v2)) < 0.001

    # Opposite vectors
    v1 = [1.0, 0.0, 0.0]
    v2 = [-1.0, 0.0, 0.0]
    assert cosine_similarity(v1, v2) == -1.0


@pytest.mark.asyncio
async def test_normalize_embedding():
    """Test embedding normalization."""
    from src.enrichment.embedding_service import normalize_embedding

    v = [3.0, 4.0]  # Length 5
    normalized = normalize_embedding(v)
    assert abs(normalized[0] - 0.6) < 0.001
    assert abs(normalized[1] - 0.8) < 0.001

    # Zero vector
    v = [0.0, 0.0]
    normalized = normalize_embedding(v)
    assert normalized == [0.0, 0.0]


@pytest.mark.asyncio
async def test_average_embeddings():
    """Test averaging embeddings."""
    from src.enrichment.embedding_service import average_embeddings, normalize_embedding

    embeddings = [
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ]
    avg = average_embeddings(embeddings)
    # Should be normalized
    assert len(avg) == 3

    # Single embedding
    avg = average_embeddings([[1.0, 2.0, 3.0]])
    assert avg == normalize_embedding([1.0, 2.0, 3.0])

    # Empty
    avg = average_embeddings([])
    assert avg == []


@pytest.mark.asyncio
async def test_cluster_embeddings():
    """Test embedding clustering."""
    from src.enrichment.embedding_service import cluster_embeddings

    # Three vectors: two similar, one different
    embeddings = [
        [1.0, 0.0, 0.0],
        [0.9, 0.1, 0.0],  # Similar to first
        [0.0, 1.0, 0.0],  # Different
    ]

    clusters = await cluster_embeddings(embeddings, threshold=0.8)
    assert len(clusters) == 2  # Two clusters
    # First cluster should have 2 items
    assert len(clusters[0]) == 2 or len(clusters[1]) == 2


@pytest.mark.asyncio
async def test_extract_key_entities():
    """Test key entity extraction for search queries."""
    from src.enrichment.pipeline import extract_key_entities

    articles = [
        RawArticle(
            id=uuid.uuid4(),
            url="https://example.com/1",
            url_hash="h1",
            title="Test 1",
            body_text="Body 1",
            source_domain="example.com",
            source_tier=SourceTier.TIER1,
            entities={"PERSON": ["John", "Jane"], "ORG": ["Acme Corp"], "GPE": ["New York"]},
        ),
        RawArticle(
            id=uuid.uuid4(),
            url="https://example.com/2",
            url_hash="h2",
            title="Test 2",
            body_text="Body 2",
            source_domain="example.com",
            source_tier=SourceTier.TIER1,
            entities={"PERSON": ["John", "Bob"], "ORG": ["Acme Corp"], "GPE": ["London"]},
        ),
    ]

    entities = extract_key_entities(articles)
    assert "John" in entities
    assert "Acme Corp" in entities
    # John and Acme Corp appear twice, should be at top


@pytest.mark.asyncio
async def test_extract_video_embeds():
    """Test video ID extraction from text."""
    from src.enrichment.media_extractor import extract_video_embeds

    text = "Watch this: https://youtube.com/watch?v=dQw4w9WgXcQ and https://vimeo.com/123456789"
    videos = extract_video_embeds(text, "https://example.com")

    assert len(videos) == 2
    assert any(v["source_id"] == "dQw4w9WgXcQ" for v in videos)
    assert any(v["source_id"] == "123456789" for v in videos)


@pytest.mark.asyncio
async def test_extract_social_embeds():
    """Test social media ID extraction from text."""
    from src.enrichment.media_extractor import extract_social_embeds

    text = "Check this tweet: https://twitter.com/user/status/1234567890 and https://bsky.app/profile/user/post/abc123"
    embeds = extract_social_embeds(text, "https://example.com")

    assert len(embeds) == 2
    assert any(e["source_id"] == "1234567890" for e in embeds)
    assert any(e["source_id"] == "abc123" for e in embeds)


@pytest.mark.asyncio
async def test_classify_embed():
    """Test embed URL classification."""
    from src.enrichment.media_extractor import classify_embed

    assert classify_embed("https://www.youtube.com/embed/abc") == "youtube"
    assert classify_embed("https://player.vimeo.com/video/123") == "vimeo"
    assert classify_embed("https://twitter.com/user/status/123") == "twitter"
    assert classify_embed("https://instagram.com/p/abc/") == "instagram"
    assert classify_embed("https://tiktok.com/@user/video/123") == "tiktok"
    assert classify_embed("https://bsky.app/profile/user/post/123") == "bluesky"
    assert classify_embed("https://unknown.com/embed") == "embed"


@pytest.mark.asyncio
async def test_deduplicate_media():
    """Test media deduplication."""
    from src.enrichment.media_extractor import deduplicate_media

    media = [
        {"url": "https://example.com/img1.jpg", "type": "image"},
        {"url": "https://example.com/img2.jpg", "type": "image"},
        {"url": "https://example.com/img1.jpg", "type": "image"},  # Duplicate
    ]

    unique = deduplicate_media(media)
    assert len(unique) == 2


@pytest.mark.asyncio
async def test_deduplicate_snippets():
    """Test snippet deduplication using minhash."""
    from src.enrichment.snippet_extractor import deduplicate_snippets

    snippets = [
        {"text": "This is a test quote about the economy", "confidence": 90, "minhash_signature": [1, 2, 3]},
        {"text": "This is a test quote about economics", "confidence": 80, "minhash_signature": [1, 2, 4]},  # Similar
        {"text": "Completely different snippet about sports", "confidence": 85, "minhash_signature": [5, 6, 7]},
    ]

    unique = deduplicate_snippets(snippets)
    # Should keep first (highest confidence) and third
    assert len(unique) == 2
    assert unique[0]["confidence"] == 90


@pytest.mark.asyncio
async def test_extract_key_quotes():
    """Test regex-based quote extraction."""
    from src.enrichment.snippet_extractor import extract_key_quotes

    text = 'He said "This is a direct quote from the official" and she added \'Another quote here\'.'
    quotes = await extract_key_quotes(text)

    assert len(quotes) >= 1
    assert "This is a direct quote from the official" in quotes or "Another quote here" in quotes


@pytest.mark.asyncio
async def test_extract_statistics():
    """Test statistics extraction."""
    from src.enrichment.snippet_extractor import extract_statistics

    text = "The economy grew by 5.2% last quarter. More than 10 million people are employed."
    stats = await extract_statistics(text)

    assert len(stats) >= 1
    # Should find percentage and large numbers


@pytest.mark.asyncio
async def test_extract_factual_claims():
    """Test factual claim extraction."""
    from src.enrichment.snippet_extractor import extract_factual_claims

    text = "Officials confirmed that the new policy will take effect next month. According to sources, the change is permanent."
    claims = await extract_factual_claims(text)

    assert len(claims) >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])