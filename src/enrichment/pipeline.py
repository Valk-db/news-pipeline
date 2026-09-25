"""Enrichment pipeline - orchestrates media, video, snippet, and embedding enrichment."""

import asyncio
import logging
from typing import List, Dict, Any, Awaitable, Tuple
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import Story, RawArticle, ReportingUnit, StoryUnitLink
from src.enrichment.media_extractor import extract_media_from_article
from src.enrichment.video_finder import find_videos_for_story
from src.enrichment.social_snippets import find_snippets_for_story
from src.enrichment.snippet_extractor import enrich_story_with_snippets
from src.enrichment.embedding_service import embed_story

logger = logging.getLogger(__name__)


# Type alias for enrichment tasks
EnrichmentTask = Tuple[str, Awaitable[Any]]


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
        story_id: Story UUID
        max_videos: Max videos to find
        max_snippets: Max social snippets to find
        enable_embeddings: Whether to generate embeddings

    Returns:
        Dict with enrichment results
    """
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
    stmt = select(Story).where(Story.id == story_id)
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
        .where(StoryUnitLink.story_id == story_id)
    )
    result = await session.execute(stmt)
    articles: List[RawArticle] = result.scalars().all()

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

    # 1. Media extraction from articles
    for article in article_data:
        if article["url"]:
            tasks.append(("media", extract_media_from_article(article["article_id"], article["url"])))

    # 2. Video finding
    story_title = " ".join([a["title"] for a in articles[:3]])
    tasks.append(("videos", find_videos_for_story(story_title, key_entities, max_videos)))

    # 3. Social snippets
    tasks.append(("social_snippets", find_snippets_for_story(story_title, key_entities, max_snippets)))

    # 4. Snippet extraction (LLM-based)
    tasks.append(("snippets", enrich_story_with_snippets(session, story_id)))

    # 5. Embeddings
    if enable_embeddings:
        texts = [a["body_text"] for a in articles if a["body_text"]]
        tasks.append(("embeddings", embed_story(story_id, texts)))

    # Execute all tasks
    if tasks:
        enrichment_results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)

        # Process results
        task_names = [t[0] for t in tasks]
        for name, result in zip(task_names, enrichment_results):
            if isinstance(result, Exception):
                logger.error(f"Enrichment task {name} failed: {result}")
                results["errors"].append(f"{name}: {result}")
            elif name == "media":
                # Media results are per-article, need to store
                pass  # Handled separately
            elif name == "videos":
                results["videos_found"] = len(result) if isinstance(result, list) else 0
            elif name == "social_snippets":
                results["social_snippets_found"] = len(result) if isinstance(result, list) else 0
            elif name == "snippets":
                results["snippets_extracted"] = result if isinstance(result, int) else 0
            elif name == "embeddings":
                results["embeddings_generated"] = 1 if result else 0

    return results


def extract_key_entities(articles: List[RawArticle]) -> List[str]:
    """Extract key entities from articles for search queries."""
    entity_counts = {}

    for article in articles:
        if article.entities:
            for entity_type, entities in article.entities.items():
                if entity_type in {"PERSON", "ORG", "GPE", "LOC", "EVENT"}:
                    for entity in entities:
                        entity_counts[entity] = entity_counts.get(entity, 0) + 1

    # Sort by frequency and return top entities
    sorted_entities = sorted(entity_counts.items(), key=lambda x: x[1], reverse=True)
    return [e[0] for e in sorted_entities[:10]]


async def enrich_stories_batch(
    session: AsyncSession,
    story_ids: List[str],
    max_videos: int = 5,
    max_snippets: int = 10,
    enable_embeddings: bool = True,
    concurrency: int = 3,
) -> List[Dict[str, Any]]:
    """
    Enrich multiple stories with concurrency control.

    Args:
        session: Database session
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
            return await enrich_story(session, story_id, max_videos, max_snippets, enable_embeddings)

    tasks = [enrich_with_semaphore(sid) for sid in story_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Handle exceptions
    processed_results = []
    for story_id, result in zip(story_ids, results):
        if isinstance(result, Exception):
            logger.error(f"Enrichment failed for story {story_id}: {result}")
            processed_results.append({
                "story_id": story_id,
                "error": str(result),
            })
        else:
            processed_results.append(result)

    return processed_results


async def enrich_recent_stories(
    session: AsyncSession,
    hours_back: int = 24,
    max_stories: int = 50,
    **kwargs,
) -> List[Dict[str, Any]]:
    """
    Enrich recent stories that don't have enrichment yet.

    Args:
        session: Database session
        hours_back: Look back this many hours
        max_stories: Maximum stories to enrich
        **kwargs: Passed to enrich_stories_batch

    Returns:
        List of enrichment results
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    # Get recent PENDING/QUEUED stories
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
    return await enrich_stories_batch(session, story_ids, **kwargs)