"""Enrichment package for multimedia and semantic enrichment."""

from src.enrichment.media_extractor import extract_media_from_article, extract_media_from_html
from src.enrichment.video_finder import find_related_videos, find_videos_for_story
from src.enrichment.social_snippets import find_social_snippets, find_snippets_for_story
from src.enrichment.snippet_extractor import (
    extract_snippets_from_article,
    extract_snippets_for_story,
    enrich_story_with_snippets,
)
from src.enrichment.embedding_service import (
    EmbeddingService,
    get_embedding_service,
    embed_article,
    embed_story,
    cluster_embeddings,
)
from src.enrichment.pipeline import enrich_story, enrich_stories_batch, enrich_recent_stories

__all__ = [
    # Media extraction
    "extract_media_from_article",
    "extract_media_from_html",
    # Video finder
    "find_related_videos",
    "find_videos_for_story",
    # Social snippets
    "find_social_snippets",
    "find_snippets_for_story",
    # Snippet extraction
    "extract_snippets_from_article",
    "extract_snippets_for_story",
    "enrich_story_with_snippets",
    # Embeddings
    "EmbeddingService",
    "get_embedding_service",
    "embed_article",
    "embed_story",
    "cluster_embeddings",
    # Pipeline
    "enrich_story",
    "enrich_stories_batch",
    "enrich_recent_stories",
]