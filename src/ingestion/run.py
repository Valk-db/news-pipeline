"""Main ingestion pipeline entry point."""

import asyncio
import sys
import os
from datetime import datetime, timezone
from src.ingestion.rss import ingest_rss_feeds
from src.ingestion.gdelt import ingest_gdelt, GDELT_TIER1_CRITICAL_DOMAINS
from src.ingestion.reddit import ingest_reddit
from src.verification.units import build_reporting_units
from src.verification.stories import build_stories
from src.verification.tiers import apply_tier1_gate
from src.shared.database import get_session, init_db
from src.schema.models import RawArticle, StatusLog
from src.shared.config import get_settings
from src.utils.ingest_stats import STATS
import json


async def log_status(session, phase: str, status: str, details: dict = None):
    """Log pipeline status to database."""
    import subprocess
    try:
        commit_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        commit_sha = None

    log = StatusLog(
        phase=phase,
        status=status,
        details=details or {},
        commit_sha=commit_sha,
    )
    session.add(log)
    await session.commit()


async def run_ingestion(dry_run: bool = False) -> dict:
    """Run the full ingestion → verification → grouping pipeline."""
    settings = get_settings()
    STATS.reset()
    results = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "phases": {},
    }

    async with get_session() as session:
        # Phase 1: Ingestion
        print("Phase 1: Ingesting articles...")
        rss_articles = await ingest_rss_feeds(settings.max_articles_per_feed)
        if settings.gdelt_enabled:
            gdelt_articles, gdelt_health = await ingest_gdelt(hours_back=24, max_per_domain=50)
        else:
            gdelt_articles, gdelt_health = [], {"succeeded": [], "failed": [], "skipped": [], "disabled": True}
        reddit_articles = await ingest_reddit(limit_per_sub=25)

        all_articles = rss_articles + gdelt_articles + reddit_articles
        print(f"  RSS: {len(rss_articles)}, GDELT: {len(gdelt_articles)}, Reddit: {len(reddit_articles)}")
        print(f"  Total fetched articles: {len(all_articles)}")

        # Filter out articles that already exist in DB (by URL hash)
        from sqlalchemy import select
        existing_url_hashes = set()
        existing_content_hashes: dict[str, set[str]] = {}  # content_hash -> set of source_domains
        if all_articles:
            url_hashes = [a.url_hash for a in all_articles]
            stmt = select(RawArticle.url_hash).where(RawArticle.url_hash.in_(url_hashes))
            result = await session.execute(stmt)
            existing_url_hashes = set(result.scalars().all())

            # Also check content hashes for exact duplicate detection, scoped by source_domain
            # We only dedupe content hashes within the same source_domain to allow
            # syndicated wire stories (same content, different publishers) to both exist
            content_hashes = [a.content_hash for a in all_articles if a.content_hash]
            if content_hashes:
                stmt = select(RawArticle.content_hash, RawArticle.source_domain).where(
                    RawArticle.content_hash.in_(content_hashes)
                )
                result = await session.execute(stmt)
                for content_hash, source_domain in result.all():
                    if content_hash not in existing_content_hashes:
                        existing_content_hashes[content_hash] = set()
                    existing_content_hashes[content_hash].add(source_domain)

        # Deduplicate: skip if URL hash OR (content hash + same source_domain) already exists
        url_dup = 0
        content_dup = 0
        new_articles = []
        for a in all_articles:
            if a.url_hash in existing_url_hashes:
                url_dup += 1
                continue
            if a.content_hash:
                domains_with_hash = existing_content_hashes.get(a.content_hash, set())
                if a.source_domain in domains_with_hash:
                    content_dup += 1
                    continue
            new_articles.append(a)

        print(f"  New articles (after dedup): {len(new_articles)}")
        print(f"  URL duplicates skipped: {url_dup}")
        print(f"  Content duplicates skipped: {content_dup}")

        # Check for tier-1 critical GDELT domains down
        tier1_critical_down = sorted(
            GDELT_TIER1_CRITICAL_DOMAINS & set(gdelt_health["failed"] + gdelt_health["skipped"])
        )
        ingest_status = "degraded" if tier1_critical_down else "ok"

        # Add extraction stats to ingestion results
        stats_snapshot = STATS.snapshot()

        results["phases"]["ingestion"] = {
            "rss": len(rss_articles),
            "gdelt": len(gdelt_articles),
            "reddit": len(reddit_articles),
            "total_fetched": len(all_articles),
            "total_new": len(new_articles),
            "url_duplicates_skipped": url_dup,
            "content_duplicates_skipped": content_dup,
            "gdelt_health": gdelt_health,
            "tier1_critical_down": tier1_critical_down,
            "extraction_stats": stats_snapshot,
        }

        if dry_run:
            print("Dry run complete.")
            return results

        # Persist new raw articles (idempotent via url_hash unique constraint)
        inserted = 0
        for art in new_articles:
            session.add(art)
        try:
            await session.commit()
            inserted = len(new_articles)
        except Exception as e:
            await session.rollback()
            # Re-query to see which already existed
            from sqlalchemy import select
            url_hashes = [art.url_hash for art in new_articles]
            if url_hashes:
                existing = await session.execute(
                    select(RawArticle.url_hash).where(RawArticle.url_hash.in_(url_hashes))
                )
                existing_hashes = {row[0] for row in existing}
                # Filter out duplicates
                new_articles = [art for art in new_articles if art.url_hash not in existing_hashes]
                if new_articles:
                    for art in new_articles:
                        session.add(art)
                    await session.commit()
                    inserted = len(new_articles)
                else:
                    inserted = 0
            else:
                inserted = 0
        await log_status(session, "ingest", ingest_status, results["phases"]["ingestion"])

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

        # Print extraction stats as markdown table for GitHub Actions summary
        if not dry_run:
            stats_md = STATS.render_markdown()
            if stats_md:
                print("\n--- INGESTION STATS ---")
                print(stats_md)
                # Write to GITHUB_STEP_SUMMARY if available
                summary_path = os.getenv("GITHUB_STEP_SUMMARY")
                if summary_path:
                    with open(summary_path, "a") as f:
                        f.write(f"\n## Ingestion Stats\n\n{stats_md}\n")

        # Exit non-zero if tier-1 critical GDELT domains are down (not dry run)
        if not dry_run:
            tier1_critical_down = results["phases"].get("ingestion", {}).get("tier1_critical_down", [])
            if tier1_critical_down:
                print(f"\nWARNING: tier-1 critical GDELT domains down: {tier1_critical_down}")
                sys.exit(1)

    except Exception as e:
        print(f"\nPipeline failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())