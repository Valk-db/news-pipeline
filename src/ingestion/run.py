"""Main ingestion pipeline entry point."""

import asyncio
import sys
from datetime import datetime, timezone
from src.ingestion.rss import ingest_rss_feeds
from src.ingestion.gdelt import ingest_gdelt
from src.ingestion.reddit import ingest_reddit
from src.verification.units import build_reporting_units
from src.verification.stories import build_stories
from src.verification.tiers import apply_tier1_gate
from src.shared.database import get_session, init_db
from src.schema.models import RawArticle, StatusLog
from src.shared.config import get_settings
import json


async def log_status(session, phase: str, status: str, details: dict = None):
    """Log pipeline status to database."""
    log = StatusLog(
        phase=phase,
        status=status,
        details=details or {},
    )
    session.add(log)
    await session.commit()


async def run_ingestion(dry_run: bool = False) -> dict:
    """Run the full ingestion → verification → grouping pipeline."""
    settings = get_settings()
    results = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "phases": {},
    }

    async with get_session() as session:
        # Phase 1: Ingestion
        print("Phase 1: Ingesting articles...")
        rss_articles = await ingest_rss_feeds(settings.max_articles_per_feed)
        gdelt_articles = await ingest_gdelt(hours_back=24, max_per_domain=50)
        reddit_articles = await ingest_reddit(limit_per_sub=25)

        all_articles = rss_articles + gdelt_articles + reddit_articles
        print(f"  RSS: {len(rss_articles)}, GDELT: {len(gdelt_articles)}, Reddit: {len(reddit_articles)}")
        print(f"  Total fetched articles: {len(all_articles)}")

        # Filter out articles that already exist in DB
        from sqlalchemy import select
        existing_hashes = set()
        if all_articles:
            url_hashes = [a.url_hash for a in all_articles]
            stmt = select(RawArticle.url_hash).where(RawArticle.url_hash.in_(url_hashes))
            result = await session.execute(stmt)
            existing_hashes = set(result.scalars().all())

        new_articles = [a for a in all_articles if a.url_hash not in existing_hashes]
        print(f"  New articles (after dedup): {len(new_articles)}")
        print(f"  Duplicates skipped: {len(all_articles) - len(new_articles)}")

        results["phases"]["ingestion"] = {
            "rss": len(rss_articles),
            "gdelt": len(gdelt_articles),
            "reddit": len(reddit_articles),
            "total_fetched": len(all_articles),
            "total_new": len(new_articles),
        }

        if dry_run:
            print("Dry run complete.")
            return results

        # Persist new raw articles
        for art in new_articles:
            session.add(art)
        await session.commit()
        await log_status(session, "ingest", "ok", results["phases"]["ingestion"])

        # Phase 2: Build reporting units (near-dup clustering)
        print("Phase 2: Building reporting units...")
        units_created = await build_reporting_units(session)
        print(f"  Reporting units created: {units_created}")
        results["phases"]["reporting_units"] = {"created": units_created}
        await log_status(session, "verify", "ok", {"units_created": units_created})

        # Phase 3: Build stories (semantic grouping)
        print("Phase 3: Building stories...")
        modified_story_ids = await build_stories(session)
        stories_created = len(modified_story_ids)
        print(f"  Stories created/modified: {stories_created}")
        results["phases"]["stories"] = {"created_or_modified": stories_created, "modified_story_ids": [str(sid) for sid in modified_story_ids]}
        await log_status(session, "group", "ok", {"stories_created_or_modified": stories_created})

        # Phase 4: Apply tier-1 gate (re-evaluate modified stories)
        print("Phase 4: Applying tier-1 gate...")
        gated = await apply_tier1_gate(session, story_ids=modified_story_ids)
        print(f"  Stories queued: {gated['queued']}, blocked: {gated['blocked']}")
        results["phases"]["gate"] = gated
        await log_status(session, "gate", "ok", gated)

    results["completed_at"] = datetime.now(timezone.utc).isoformat()
    return results


async def main():
    """Main entry point."""
    dry_run = "--dry-run" in sys.argv

    # Initialize DB
    await init_db()

    try:
        results = await run_ingestion(dry_run=dry_run)
        print("\nPipeline completed successfully.")
        print(json.dumps(results, indent=2))
    except Exception as e:
        print(f"\nPipeline failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())