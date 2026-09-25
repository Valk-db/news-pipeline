"""Enrichment pipeline - orchestrates media, video, snippet, and embedding enrichment."""

import asyncio
import logging
import uuid
from typing import List, Dict, Any, Awaitable, Tuple
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import (
    Story, RawArticle, ReportingUnit, StoryUnitLink,
    MediaAsset, StoryEmbedding
)
from src.enrichment.media_extractor import extract_media_from_article
from src.enrichment.video_finder import find_videos_for_story
from src.enrichment.social_snippets import find_snippets_for_story
from src.enrichment.snippet_extractor import enrich_story_with_snippets
from src.enrichment.embedding_service import embed_story

logger = logging.getLogger(__name__)


# Type alias for enrichment tasks
# For media tasks, include article_id: (kind, article_id, coroutine)
# For other tasks: (kind, None, coroutine)
EnrichmentTask = Tuple[str, Any, Awaitable[Any]]


async def enrich_story(
    session: AsyncSession,
    story_id: str,
    max_videos: int = 5,
    max_snippets: int = 10,
    enable_embeddings: bool = True,
) -> Dict[str, Any]:
    """
    Enrich a single story with all available enrichment.

    Args:
        session: Database session
        story_id: Story UUID (as string)
        max_videos: Max videos to find
        max_snippets: Max social snippets to find
        enable_embeddings: Whether to generate embeddings

    Returns:
        Dict with enrichment results
    """
    # Convert story_id string to UUID for database queries
    story_uuid = uuid.UUID(story_id)

    results: Dict[str, Any] = {
        "story_id": story_id,
        "media_assets": 0,
        "videos_found": 0,
        "social_snippets_found": 0,
        "snippets_extracted": 0,
        "embeddings_generated": 0,
        "errors": [],
    }

    # Get story details
    stmt = select(Story).where(Story.id == story_uuid)
    result = await session.execute(stmt)
    story = result.scalar_one_or_none()
    if not story:
        results["errors"].append("Story not found")
        return results

    # Get all articles in story
    stmt = (
        select(RawArticle)
        .join(ReportingUnit, RawArticle.id == ReportingUnit.representative_article_id)
        .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .where(StoryUnitLink.story_id == story_uuid)
    )
    result = await session.execute(stmt)
    articles: List[RawArticle] = list(result.scalars().all())

    if not articles:
        results["errors"].append("No articles in story")
        return results

    # Prepare article data for enrichment
    article_data: List[Dict[str, Any]] = []
    for article in articles:
        article_data.append({
            "article_id": article.id,
            "title": article.title,
            "body_text": article.body_text,
            "url": article.url,
            "source_domain": article.source_domain,
            "entities": article.entities,
        })

    # Extract key entities for search queries
    key_entities = extract_key_entities(articles)

    # Run enrichment tasks in parallel
    tasks: List[EnrichmentTask] = []

    # 1. Media extraction from articles - include article_id for each
    for article_dict in article_data:
        if article_dict["url"]:
            tasks.append(("media", article_dict["article_id"], extract_media_from_article(article_dict["article_id"], article_dict["url"])))

    # 2. Video finding
    story_title = " ".join([a["title"] for a in article_data[:3]])
    tasks.append(("videos", None, find_videos_for_story(story_title, key_entities, max_videos)))

    # 3. Social snippets
    tasks.append(("social_snippets", None, find_snippets_for_story(story_title, key_entities, max_snippets)))

    # 4. Snippet extraction (LLM-based)
    tasks.append(("snippets", None, enrich_story_with_snippets(session, story_id)))

    # 5. Embeddings
    if enable_embeddings:
        texts = [a["body_text"] for a in article_data if a["body_text"]]
        tasks.append(("embeddings", None, embed_story(story_id, texts)))

    # Execute all tasks
    if tasks:
        # Separate coroutines from task metadata
        task_coroutines = [t[2] for t in tasks]
        enrichment_results = await asyncio.gather(*task_coroutines, return_exceptions=True)

        # Process results - match by index
        for i, (kind, article_id, _) in enumerate(tasks):
            result = enrichment_results[i]
            if isinstance(result, Exception):
                logger.error(f"Enrichment task {kind} failed: {result}")
                results["errors"].append(f"{kind}: {result}")
                continue

            # Process each kind of result
            if kind == "media":
                # Media results are per-article
                if isinstance(result, dict):
                    article_media_count = await _persist_media_assets(
                        session, story_uuid, article_id, result
                    )
                    results["media_assets"] += article_media_count

            elif kind == "videos":
                if isinstance(result, list):
                    video_count = await _persist_video_assets(session, story_uuid, result)
                    results["videos_found"] = video_count

            elif kind == "social_snippets":
                if isinstance(result, list):
                    social_count = await _persist_social_snippets(session, story_uuid, result)
                    results["social_snippets_found"] = social_count

            elif kind == "snippets":
                # enrich_story_with_snippets already commits internally
                results["snippets_extracted"] = result if isinstance(result, int) else 0

            elif kind == "embeddings":
                if result and isinstance(result, dict):
                    await _persist_story_embedding(session, story_uuid, result)
                    results["embeddings_generated"] = 1

        # Commit all new MediaAsset and StoryEmbedding rows at once
        try:
            await session.commit()
        except Exception as e:
            logger.error(f"Failed to commit enrichment results for story {story_id}: {e}")
            results["errors"].append(f"commit: {e}")
            await session.rollback()

    return results


async def _persist_media_assets(
    session: AsyncSession,
    story_id: uuid.UUID,
    article_id: str,
    media_result: Dict[str, List[Dict[str, Any]]]
) -> int:
    """Persist media assets from article extraction to MediaAsset table."""
    count = 0
    media_type_map = {
        "images": MediaAsset.MediaType.IMAGE,
        "videos": MediaAsset.MediaType.VIDEO,
        "embeds": MediaAsset.MediaType.EMBED,
        "audio": MediaAsset.MediaType.AUDIO,
    }

    for media_type_key, media_items in media_result.items():
        if not isinstance(media_items, list):
            continue

        media_type = media_type_map.get(media_type_key)
        if not media_type:
            continue

        for item in media_items:
            # Build meta_data with any extra fields not in the model
            meta_data = {}
            for key, value in item.items():
                if key not in {
                    "url", "alt_text", "width", "height", "source", "source_id",
                    "thumbnail_url", "duration_seconds", "media_type", "type"
                }:
                    meta_data[key] = value

            asset = MediaAsset(
                article_id=article_id,
                story_id=story_id,
                media_type=media_type,
                url=item.get("url", ""),
                thumbnail_url=item.get("thumbnail_url"),
                alt_text=item.get("alt_text"),
                width=item.get("width"),
                height=item.get("height"),
                duration_seconds=item.get("duration_seconds"),
                source=item.get("source"),
                source_id=item.get("source_id"),
                meta_data=meta_data if meta_data else None,
            )
            session.add(asset)
            count += 1

    return count


async def _persist_video_assets(
    session: AsyncSession,
    story_id: uuid.UUID,
    videos: List[Dict[str, Any]]
) -> int:
    """Persist video search results to MediaAsset table."""
    count = 0
    for video in videos:
        meta_data = {}
        for key, value in video.items():
            if key not in {
                "video_id", "title", "description", "thumbnail_url", "channel_title",
                "channel_id", "published_at", "embed_url", "watch_url", "source",
                "duration_seconds", "view_count", "like_count", "comment_count"
            }:
                meta_data[key] = value

        asset = MediaAsset(
            story_id=story_id,
            article_id=None,
            media_type=MediaAsset.MediaType.VIDEO,
            url=video.get("embed_url") or video.get("watch_url", ""),
            thumbnail_url=video.get("thumbnail_url"),
            alt_text=video.get("title"),
            duration_seconds=video.get("duration_seconds"),
            source=video.get("source"),
            source_id=video.get("video_id"),
            meta_data=meta_data if meta_data else None,
        )
        session.add(asset)
        count += 1

    return count


async def _persist_social_snippets(
    session: AsyncSession,
    story_id: uuid.UUID,
    snippets: List[Dict[str, Any]]
) -> int:
    """Persist social media snippets to MediaAsset table."""
    count = 0
    for snippet in snippets:
        meta_data = {}
        for key, value in snippet.items():
            if key not in {
                "post_id", "text", "author_username", "author_verified", "created_at",
                "retweet_count", "like_count", "reply_count", "quote_count", "url",
                "source", "platform", "author_handle", "author_did", "author_avatar",
                "repost_count", "title", "subreddit", "score", "upvote_ratio",
                "num_comments", "author"
            }:
                meta_data[key] = value

        asset = MediaAsset(
            story_id=story_id,
            article_id=None,
            media_type=MediaAsset.MediaType.EMBED,
            url=snippet.get("url", ""),
            alt_text=snippet.get("text", "")[:300] if snippet.get("text") else None,
            source=snippet.get("source") or snippet.get("platform"),
            source_id=snippet.get("post_id"),
            meta_data=meta_data if meta_data else None,
        )
        session.add(asset)
        count += 1

    return count


async def _persist_story_embedding(
    session: AsyncSession,
    story_id: uuid.UUID,
    embedding_result: Dict[str, Any]
) -> None:
    """Persist story embedding to StoryEmbedding table."""
    embedding = StoryEmbedding(
        story_id=story_id,
        model=embedding_result.get("model", "unknown"),
        embedding=embedding_result.get("embedding", []),
        dimensions=embedding_result.get("dimensions", 0),
    )
    session.add(embedding)


def extract_key_entities(articles: List[RawArticle]) -> List[str]:
    """Extract key entities from articles for search queries."""
    entity_counts = {}

    for article in articles:
        entities = getattr(article, "entities", None)
        if entities:
            for entity_type, entity_list in entities.items():
                if entity_type in {"PERSON", "ORG", "GPE", "LOC", "EVENT"}:
                    for entity in entity_list:
                        entity_counts[entity] = entity_counts.get(entity, 0) + 1

    # Sort by frequency and return top entities
    sorted_entities = sorted(entity_counts.items(), key=lambda x: x[1], reverse=True)
    return [e[0] for e in sorted_entities[:10]]


async def enrich_stories_batch(
    session_factory,
    story_ids: List[str],
    max_videos: int = 5,
    max_snippets: int = 10,
    enable_embeddings: bool = True,
    concurrency: int = 3,
) -> List[Dict[str, Any]]:
    """
    Enrich multiple stories with concurrency control.

    Each story gets its own session to prevent transaction rollback cascading.

    Args:
        session_factory: Async session factory (e.g., async_session_maker)
        story_ids: List of story UUIDs
        max_videos: Max videos per story
        max_snippets: Max social snippets per story
        enable_embeddings: Whether to generate embeddings
        concurrency: Max concurrent enrichments

    Returns:
        List of enrichment results
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def enrich_with_semaphore(story_id):
        async with semaphore:
            # Create a new session for each story to isolate transactions
            async with session_factory() as session:
                return await enrich_story(session, story_id, max_videos, max_snippets, enable_embeddings)

    tasks = [enrich_with_semaphore(sid) for sid in story_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Handle exceptions - ensure same structure as success results
    processed_results = []
    for story_id, result in zip(story_ids, results):
        if isinstance(result, Exception):
            logger.error(f"Enrichment failed for story {story_id}: {result}")
            processed_results.append({
                "story_id": story_id,
                "media_assets": 0,
                "videos_found": 0,
                "social_snippets_found": 0,
                "snippets_extracted": 0,
                "embeddings_generated": 0,
                "errors": [str(result)],
            })
        else:
            processed_results.append(result)

    return processed_results


async def enrich_recent_stories(
    session_factory,
    hours_back: int = 24,
    max_stories: int = 50,
    **kwargs,
) -> List[Dict[str, Any]]:
    """
    Enrich recent stories that don't have enrichment yet.

    Uses a session factory to create isolated sessions per story.

    Args:
        session_factory: Async session factory (e.g., async_session_maker)
        hours_back: Look back this many hours
        max_stories: Maximum stories to enrich
        **kwargs: Passed to enrich_stories_batch

    Returns:
        List of enrichment results
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    # Get recent PENDING/QUEUED stories - use a temporary session for this query
    async with session_factory() as session:
        stmt = (
            select(Story)
            .where(Story.status.in_([Story.Status.PENDING, Story.Status.QUEUED]))
            .where(Story.created_at >= cutoff)
            .order_by(Story.created_at.desc())
            .limit(max_stories)
        )
        result = await session.execute(stmt)
        stories = result.scalars().all()

    story_ids = [str(s.id) for s in stories]

    if not story_ids:
        logger.info("No recent stories to enrich")
        return []

    logger.info(f"Enriching {len(story_ids)} recent stories")
    return await enrich_stories_batch(session_factory, story_ids, **kwargs)