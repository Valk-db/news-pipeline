"""Cleanup job for stale stories and orphaned reporting units."""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import Story, ReportingUnit, StoryUnitLink, Story, StatusLog
from src.shared.config import get_settings


@dataclass
class CleanupResult:
    """Result of a cleanup run."""
    stories_expired: int = 0
    orphaned_units_removed: int = 0
    stale_links_removed: int = 0
    details: List[str] = None

    def __post_init__(self):
        if self.details is None:
            self.details = []
        else:
            self.details = list(self.details)


async def cleanup_stale_stories(
    session: AsyncSession,
    pending_hours: int = 120,      # PENDING stories older than this become EXPIRED
    blocked_hours: int = 168,     # BLOCKED stories older than this become EXPIRED (1 week)
    queued_hours: int = 0,        # QUEUED stories older than this become EXPIRED (0 = disabled)
) -> CleanupResult:
    """
    Mark stale PENDING/BLOCKED/QUEUED stories as EXPIRED.

    PENDING: story waiting for curator action - expire after 5 days (curator didn't act)
    BLOCKED: story failed tier-1 gate - expire after 7 days (unlikely to get new coverage)
    QUEUED: story passed gate but not approved - OPT-IN only (default disabled, 0 = never expire)
    POSTED/REJECTED: never expired (curator explicitly acted)
    """
    result = CleanupResult()
    settings = get_settings()
    now = datetime.now(timezone.utc)

    # Expire PENDING stories
    pending_cutoff = now - timedelta(hours=pending_hours)
    stmt = select(Story).where(
        Story.status == Story.Status.PENDING,
        Story.updated_at < pending_cutoff
    )
    pending_result = await session.execute(stmt)
    pending_stories = pending_result.scalars().all()

    for story in pending_stories:
        story.status = Story.Status.EXPIRED
        story.gate_reason = f"Auto-expired: PENDING for >{pending_hours}h without curation"
        story.updated_at = now
        result.stories_expired += 1
        result.details.append(f"Expired PENDING story {story.id} (updated: {story.updated_at})")

    # Expire BLOCKED stories
    blocked_cutoff = now - timedelta(hours=blocked_hours)
    stmt = select(Story).where(
        Story.status == Story.Status.BLOCKED,
        Story.updated_at < blocked_cutoff
    )
    blocked_result = await session.execute(stmt)
    blocked_stories = blocked_result.scalars().all()

    for story in blocked_stories:
        story.status = Story.Status.EXPIRED
        story.gate_reason = f"Auto-expired: BLOCKED for >{blocked_hours}h without new coverage"
        story.updated_at = now
        result.stories_expired += 1
        result.details.append(f"Expired BLOCKED story {story.id} (updated: {story.updated_at})")

    # Expire QUEUED stories (opt-in only, default disabled)
    if queued_hours > 0:
        queued_cutoff = now - timedelta(hours=queued_hours)
        stmt = select(Story).where(
            Story.status == Story.Status.QUEUED,
            Story.updated_at < queued_cutoff
        )
        queued_result = await session.execute(stmt)
        queued_stories = queued_result.scalars().all()

        for story in queued_stories:
            story.status = Story.Status.EXPIRED
            story.gate_reason = f"Auto-expired: QUEUED for >{queued_hours}h without curator action"
            story.updated_at = now
            result.stories_expired += 1
            result.details.append(f"Expired QUEUED story {story.id} (updated: {story.updated_at})")

    await session.flush()
    return result


async def cleanup_orphaned_reporting_units(session: AsyncSession) -> CleanupResult:
    """
    Remove reporting units that have no articles (shouldn't happen) and no story links.
    These are orphaned clusters from ingestion that never got grouped.
    """
    result = CleanupResult()

    # Find units with no story links AND no articles (article_count = 0 or missing rep article)
    stmt = (
        select(ReportingUnit)
        .outerjoin(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .where(StoryUnitLink.id.is_(None))
    )
    result_exec = await session.execute(stmt)
    unlinked_units = result_exec.scalars().all()

    # Only remove units that are old (not from current run)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for unit in unlinked_units:
        if unit.created_at < cutoff:
            await session.delete(unit)
            result.orphaned_units_removed += 1
            result.details.append(f"Removed orphaned reporting unit {unit.id} (created: {unit.created_at})")

    await session.flush()
    return result


async def cleanup_stale_story_links(session: AsyncSession) -> CleanupResult:
    """
    Remove StoryUnitLink rows pointing to deleted stories or units.
    (Should be handled by CASCADE, but defensive cleanup.)
    """
    result = CleanupResult()

    # Find links where story or unit doesn't exist
    stmt = select(StoryUnitLink)
    result_exec = await session.execute(stmt)
    links = result_exec.scalars().all()

    for link in links:
        story_stmt = select(Story).where(Story.id == link.story_id)
        story_result = await session.execute(story_stmt)
        story = story_result.scalar_one_or_none()

        unit_stmt = select(ReportingUnit).where(ReportingUnit.id == link.unit_id)
        unit_result = await session.execute(unit_stmt)
        unit = unit_result.scalar_one_or_none()

        if not story or not unit:
            await session.delete(link)
            result.stale_links_removed += 1
            result.details.append(f"Removed stale link {link.id} (story: {link.story_id}, unit: {link.unit_id})")

    await session.flush()
    return result


async def run_cleanup(
    session: AsyncSession,
    pending_hours: int = 120,
    blocked_hours: int = 168,
    queued_hours: int = 0,
) -> CleanupResult:
    """
    Run all cleanup tasks in one transaction.
    Returns aggregated results.
    """
    total = CleanupResult()

    # 1. Expire stale stories
    story_result = await cleanup_stale_stories(session, pending_hours, blocked_hours, queued_hours)
    total.stories_expired = story_result.stories_expired
    total.details.extend(story_result.details)

    # 2. Remove orphaned reporting units
    unit_result = await cleanup_orphaned_reporting_units(session)
    total.orphaned_units_removed = unit_result.orphaned_units_removed
    total.details.extend(unit_result.details)

    # 3. Clean stale links (defensive)
    link_result = await cleanup_stale_story_links(session)
    total.stale_links_removed = link_result.stale_links_removed
    total.details.extend(link_result.details)

    # Log the cleanup run
    await session.execute(
        StatusLog.__table__.insert().values(
            phase="cleanup",
            status="ok",
            details={
                "stories_expired": total.stories_expired,
                "orphaned_units_removed": total.orphaned_units_removed,
                "stale_links_removed": total.stale_links_removed,
            }
        )
    )

    await session.commit()
    return total