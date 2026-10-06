"""Database retention job to keep the Supabase free tier (500MB) from filling up.

Everything this job touches is derived data, never a source of truth:

* Embeddings are recomputed from article text via src/enrichment/embedding_service.py,
  so article_embeddings / story_embeddings rows can be dropped freely.
* Article bodies are set to NULL, never deleted. url, url_hash, content_hash, title,
  entities, minhash_signature and every other dedup input stay intact, so ingestion
  dedup keeps rejecting re-fetches of old URLs.

raw_articles rows, stories and hashes are NEVER hard-deleted here. Story expiry
(PENDING > 5d, BLOCKED > 7d -> EXPIRED) stays owned by src/verification/cleanup.py;
this job only reacts to the EXPIRED status that cleanup already assigned.

Note: reclaiming rows does not shrink the Postgres file by itself -- the freed space
goes back to the free space map and is reused by later writes, so the numbers below
are "space made available", not "disk returned to the OS".
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import List, Optional, Tuple

from sqlalchemy import Text, and_, cast, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.schema.models import (
    ArticleEmbedding,
    RawArticle,
    StatusLog,
    Story,
    StoryEmbedding,
    StoryUnitLink,
)

logger = logging.getLogger(__name__)

# Reclaimed-space estimate is based on the on-disk text length of the dropped
# columns. For a JSON float array that is ~10-12 chars per dimension (sign, decimal
# point, digits) plus brackets/commas, so a 384-dim embedding lands around 4-5KB.
DEFAULT_RETENTION_DAYS = 90


@dataclass
class RetentionResult:
    """Rows reclaimed by a retention run (embeddings deleted, bodies trimmed)."""

    article_embeddings_deleted: int = 0
    story_embeddings_deleted: int = 0
    raw_articles_body_text_cleared: int = 0
    estimated_bytes_reclaimed: int = 0
    details: List[str] = None

    def __post_init__(self):
        if self.details is None:
            self.details = []
        else:
            self.details = list(self.details)

    @property
    def embeddings_deleted(self) -> int:
        return self.article_embeddings_deleted + self.story_embeddings_deleted

    def summary(self) -> str:
        """One-line summary for logs and CLI output."""
        return (
            f"article_embeddings deleted: {self.article_embeddings_deleted}, "
            f"story_embeddings deleted: {self.story_embeddings_deleted}, "
            f"body_text cleared: {self.raw_articles_body_text_cleared}, "
            f"estimated reclaimed: {self.estimated_bytes_reclaimed / (1024 * 1024):.2f} MB"
        )


def _resolve_days(retention_days: int, override: Optional[int], name: str) -> int:
    """Resolve a per-target retention window, defaulting to retention_days."""
    days = retention_days if override is None else override
    if days <= 0:
        raise ValueError(
            f"{name} must be a positive number of days (got {days}); "
            "refusing to run retention with a non-positive window"
        )
    return days


async def _measure(
    session: AsyncSession,
    model,
    payload_column,
    where=None,
) -> Tuple[int, int]:
    """Return (row_count, total_text_bytes) for the rows matching `where`."""
    stmt = select(
        func.count(model.id),
        func.coalesce(func.sum(func.length(cast(payload_column, Text))), 0),
    )
    if where is not None:
        stmt = stmt.where(where)
    row = (await session.execute(stmt)).one()
    return int(row[0] or 0), int(row[1] or 0)


async def _sample_ids(session: AsyncSession, column, where=None, limit: int = 5) -> List[str]:
    """Grab a few ids from a matching set so log lines name actual rows."""
    if limit <= 0:
        return []
    stmt = select(column)
    if where is not None:
        stmt = stmt.where(where)
    rows = (await session.execute(stmt.limit(limit))).scalars().all()
    return [str(value) for value in rows]


async def delete_stale_article_embeddings(
    session: AsyncSession,
    cutoff: datetime,
    dry_run: bool = False,
    sample_limit: int = 5,
) -> Tuple[int, int, List[str]]:
    """Delete article_embeddings rows created before `cutoff`."""
    where = ArticleEmbedding.created_at < cutoff
    count, nbytes = await _measure(session, ArticleEmbedding, ArticleEmbedding.embedding, where)
    if count == 0:
        return 0, 0, []
    ids = await _sample_ids(session, ArticleEmbedding.id, where, sample_limit)
    if not dry_run:
        await session.execute(delete(ArticleEmbedding).where(where))
        await session.flush()
    return count, nbytes, ids


async def delete_stale_story_embeddings(
    session: AsyncSession,
    cutoff: datetime,
    dry_run: bool = False,
    sample_limit: int = 5,
) -> Tuple[int, int, List[str]]:
    """Delete story_embeddings rows created before `cutoff`."""
    where = StoryEmbedding.created_at < cutoff
    count, nbytes = await _measure(session, StoryEmbedding, StoryEmbedding.embedding, where)
    if count == 0:
        return 0, 0, []
    ids = await _sample_ids(session, StoryEmbedding.id, where, sample_limit)
    if not dry_run:
        await session.execute(delete(StoryEmbedding).where(where))
        await session.flush()
    return count, nbytes, ids


async def delete_expired_story_embeddings(
    session: AsyncSession,
    dry_run: bool = False,
    sample_limit: int = 5,
) -> Tuple[int, int, int, List[str], List[str]]:
    """
    Delete embeddings owned by EXPIRED stories regardless of age.

    A story is expired by cleanup (PENDING > 5d / BLOCKED > 7d); nothing will be
    posted from it, so its vectors are dead weight. Story embeddings go directly;
    article embeddings are reached through StoryUnitLink -> reporting_units ->
    raw_articles.reporting_unit_id (set for every member article in units.py).

    Returns (article_count, story_count, bytes, article_ids, story_ids).
    """
    expired_story_ids = select(Story.id).where(Story.status == Story.Status.EXPIRED)
    expired_unit_ids = select(StoryUnitLink.unit_id).where(
        StoryUnitLink.story_id.in_(expired_story_ids)
    )
    expired_article_ids = select(RawArticle.id).where(
        RawArticle.reporting_unit_id.in_(expired_unit_ids)
    )

    story_where = StoryEmbedding.story_id.in_(expired_story_ids)
    article_where = ArticleEmbedding.article_id.in_(expired_article_ids)

    story_count, story_bytes = await _measure(
        session, StoryEmbedding, StoryEmbedding.embedding, story_where
    )
    article_count, article_bytes = await _measure(
        session, ArticleEmbedding, ArticleEmbedding.embedding, article_where
    )
    story_ids = await _sample_ids(session, StoryEmbedding.id, story_where, sample_limit)
    article_ids = await _sample_ids(session, ArticleEmbedding.id, article_where, sample_limit)
    if not dry_run:
        if story_count:
            await session.execute(delete(StoryEmbedding).where(story_where))
        if article_count:
            await session.execute(delete(ArticleEmbedding).where(article_where))
        await session.flush()

    return article_count, story_count, story_bytes + article_bytes, article_ids, story_ids


async def clear_stale_body_text(
    session: AsyncSession,
    cutoff: datetime,
    dry_run: bool = False,
    sample_limit: int = 5,
) -> Tuple[int, int, List[str]]:
    """
    NULL out body_text on articles fetched before `cutoff`.

    This is an UPDATE, not a DELETE: the row (and therefore url_hash / content_hash)
    stays so dedup still recognises URLs seen before the trim. summary, entities and
    minhash_signature are left alone -- they are small and still useful downstream.
    """
    where = and_(
        RawArticle.fetched_at < cutoff,
        RawArticle.body_text.isnot(None),
    )
    count, nbytes = await _measure(session, RawArticle, RawArticle.body_text, where)
    if count == 0:
        return 0, 0, []
    ids = await _sample_ids(session, RawArticle.id, where, sample_limit)
    if not dry_run:
        await session.execute(update(RawArticle).where(where).values(body_text=None))
        await session.flush()
    return count, nbytes, ids


async def run_retention(
    session: AsyncSession,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    embedding_retention_days: Optional[int] = None,
    body_text_retention_days: Optional[int] = None,
    drop_expired_story_embeddings: bool = True,
    trim_body_text: bool = True,
    sample_limit: int = 5,
    dry_run: bool = False,
    write_status_log: bool = True,
    commit: bool = True,
) -> RetentionResult:
    """
    Reclaim space from derived tables without touching anything dedup depends on.

    Args:
        session: async session (caller owns it; run commits unless commit=False)
        retention_days: default window for everything below (default 90)
        embedding_retention_days: override for embeddings, defaults to retention_days
        body_text_retention_days: override for body text, defaults to retention_days
        drop_expired_story_embeddings: also drop embeddings of EXPIRED stories at any age
        trim_body_text: set body_text to NULL on old articles (keep the rows)
        sample_limit: how many row ids to name per log line (0 = counts only)
        dry_run: count and estimate only, write nothing
        write_status_log: append a StatusLog row for the run
        commit: commit at the end (False leaves it to the caller)

    Returns a RetentionResult with per-table counts and a reclaimed-space estimate.
    """
    embedding_days = _resolve_days(
        retention_days, embedding_retention_days, "embedding_retention_days"
    )
    body_days = _resolve_days(retention_days, body_text_retention_days, "body_text_retention_days")

    result = RetentionResult()
    now = datetime.now(UTC)
    embedding_cutoff = now - timedelta(days=embedding_days)
    body_cutoff = now - timedelta(days=body_days)

    logger.info(
        f"Retention run starting: retention_days={retention_days} "
        f"(embeddings {embedding_days}d, body_text {body_days}d), "
        f"embedding_cutoff={embedding_cutoff.isoformat()}, body_cutoff={body_cutoff.isoformat()}, "
        f"drop_expired_story_embeddings={drop_expired_story_embeddings}, "
        f"trim_body_text={trim_body_text}, dry_run={dry_run}"
    )

    deleted, nbytes, ids = await delete_stale_article_embeddings(
        session, embedding_cutoff, dry_run=dry_run, sample_limit=sample_limit
    )
    result.article_embeddings_deleted += deleted
    result.estimated_bytes_reclaimed += nbytes
    logger.info(
        f"article_embeddings older than {embedding_cutoff.date()}: "
        f"{deleted} rows (~{nbytes / (1024 * 1024):.2f} MB){_id_suffix(ids)}"
    )
    result.details.append(f"Deleted {deleted} article_embeddings older than {embedding_cutoff.date()}")

    deleted, nbytes, ids = await delete_stale_story_embeddings(
        session, embedding_cutoff, dry_run=dry_run, sample_limit=sample_limit
    )
    result.story_embeddings_deleted += deleted
    result.estimated_bytes_reclaimed += nbytes
    logger.info(
        f"story_embeddings older than {embedding_cutoff.date()}: "
        f"{deleted} rows (~{nbytes / (1024 * 1024):.2f} MB){_id_suffix(ids)}"
    )
    result.details.append(f"Deleted {deleted} story_embeddings older than {embedding_cutoff.date()}")

    if drop_expired_story_embeddings:
        article_count, story_count, nbytes, article_ids, story_ids = (
            await delete_expired_story_embeddings(
                session, dry_run=dry_run, sample_limit=sample_limit
            )
        )
        result.article_embeddings_deleted += article_count
        result.story_embeddings_deleted += story_count
        result.estimated_bytes_reclaimed += nbytes
        logger.info(
            f"Embeddings of EXPIRED stories (any age): {story_count} story_embeddings, "
            f"{article_count} article_embeddings (~{nbytes / (1024 * 1024):.2f} MB); "
            f"stories={_id_suffix(story_ids)} articles={_id_suffix(article_ids)}"
        )
        result.details.append(
            f"Deleted {story_count} story_embeddings and {article_count} article_embeddings "
            f"belonging to EXPIRED stories"
        )
    else:
        logger.info("Skipping EXPIRED story embeddings (drop_expired_story_embeddings=False)")

    if trim_body_text:
        count, nbytes, ids = await clear_stale_body_text(
            session, body_cutoff, dry_run=dry_run, sample_limit=sample_limit
        )
        result.raw_articles_body_text_cleared += count
        result.estimated_bytes_reclaimed += nbytes
        logger.info(
            f"body_text NULLed on articles fetched before {body_cutoff.date()}: "
            f"{count} rows (~{nbytes / (1024 * 1024):.2f} MB){_id_suffix(ids)} "
            "-- url/title/hashes/metadata kept for dedup"
        )
        result.details.append(
            f"Cleared body_text on {count} raw_articles fetched before {body_cutoff.date()}"
        )
    else:
        logger.info("Skipping body_text trimming (trim_body_text=False)")

    logger.info(f"Retention run complete{' (dry run)' if dry_run else ''}: {result.summary()}")

    if write_status_log and not dry_run:
        await session.execute(
            StatusLog.__table__.insert().values(
                phase="retention",
                status="ok",
                details={
                    "retention_days": retention_days,
                    "embedding_retention_days": embedding_days,
                    "body_text_retention_days": body_days,
                    "article_embeddings_deleted": result.article_embeddings_deleted,
                    "story_embeddings_deleted": result.story_embeddings_deleted,
                    "raw_articles_body_text_cleared": result.raw_articles_body_text_cleared,
                    "estimated_bytes_reclaimed": result.estimated_bytes_reclaimed,
                },
            )
        )

    if commit and not dry_run:
        await session.commit()
    return result


def _id_suffix(ids: List[str]) -> str:
    """Render sampled ids for a log line, e.g. ' [id1, id2]'."""
    return f" [{', '.join(ids)}]" if ids else ""