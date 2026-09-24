"""Main ingestion pipeline entry point."""

import asyncio
import sys
import os
from datetime import datetime, timezone
from src.ingestion.rss import ingest_rss_feeds
from src.ingestion.gdelt import ingest_gdelt, GDELT_TIER1_CRITICAL_DOMAINS
from src.ingestion.reddit import ingest_reddit
from src.ingestion.source_registry import get_enabled_sources_by_tier, SourceTier
from src.ingestion.tiered_scheduler import get_scheduler
from src.verification.units import build_reporting_units
from src.verification.stories import build_stories
from src.verification.tiers import apply_tier1_gate, recompute_story_counters
from src.shared.database import get_session, init_db
from src.schema.models import RawArticle, StatusLog, Story
from src.shared.config import get_settings
from src.utils.ingest_stats import STATS
import json
from sqlalchemy import select


def dedupe_articles(all_articles, existing_url_hashes, existing_content_hashes):
    """
    Return (new_articles, url_dup, content_dup). Input order decides which copy wins.
    """
    new_articles = []
    url_dup = 0
    content_dup = 0
    batch_url_hashes: set[str] = set()
    batch_content_keys: set[tuple[str, str]] = set()
    for a in all_articles:
        if a.url_hash in existing_url_hashes or a.url_hash in batch_url_hashes:
            url_dup += 1
            continue
        if a.content_hash:
            key = (a.content_hash, a.source_domain)
            if a.source_domain in existing_content_hashes.get(a.content_hash, set()) or key in batch_content_keys:
                content_dup += 1
                continue
            batch_content_keys.add(key)
        batch_url_hashes.add(a.url_hash)
        new_articles.append(a)
    return new_articles, url_dup, content_dup


async def log_status(session, phase: str, status: str, details: dict = None):
    """Log pipeline status to database."""
    import os
    try:
        git_dir = ".git"
        head_path = os.path.join(git_dir, "HEAD")
        if os.path.exists(head_path):
            with open(head_path) as f:
                head_content = f.read().strip()
            if head_content.startswith("ref: "):
                ref_path = os.path.join(git_dir, head_content[5:])
                with open(ref_path) as f:
                    commit_sha = f.read().strip()
            else:
                commit_sha = head_content
        else:
            commit_sha = None
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


async def run_ingestion(dry_run: bool = False, tiers: list[SourceTier] | None = None) -> dict:
    """Run the full ingestion → verification → grouping pipeline.

    Args:
        dry_run: If True, don't persist to database
        tiers: If specified, only ingest these tiers. Default: all tiers.
    """
    settings = get_settings()
    STATS.reset()
    results = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "phases": {},
    }

    # Default to all tiers if not specified
    if tiers is None:
        tiers = [SourceTier.TIER1, SourceTier.TIER2, SourceTier.TIER3, SourceTier.TIER4]

    async with get_session() as session:
        # Phase 1: Ingestion
        print("Phase 1: Ingesting articles...")
        all_articles = []

        # Ingest tier-1 sources (RSS)
        if SourceTier.TIER1 in tiers:
            from src.ingestion.source_registry import get_enabled_sources_by_tier
            tier1_sources = get_enabled_sources_by_tier(SourceTier.TIER1)
            rss_articles = await ingest_rss_feeds(settings.max_articles_per_feed, tier1_sources)
            all_articles.extend(rss_articles)
            print(f"  Tier-1 RSS: {len(rss_articles)}")

        # Ingest tier-2 sources (RSS)
        if SourceTier.TIER2 in tiers:
            from src.ingestion.source_registry import get_enabled_sources_by_tier
            tier2_sources = get_enabled_sources_by_tier(SourceTier.TIER2)
            tier2_articles = await ingest_rss_feeds(settings.max_articles_per_feed, tier2_sources)
            all_articles.extend(tier2_articles)
            print(f"  Tier-2 RSS: {len(tier2_articles)}")

        # GDELT (tier-1 domains only, currently disabled)
        if settings.gdelt_enabled and SourceTier.TIER1 in tiers:
            gdelt_articles, gdelt_health = await ingest_gdelt(hours_back=24, max_per_domain=50)
        else:
            gdelt_articles, gdelt_health = [], {"succeeded": [], "failed": [], "skipped": [], "disabled": True}
        all_articles.extend(gdelt_articles)
        print(f"  GDELT: {len(gdelt_articles)}")

        # Reddit (tier-3)
        if SourceTier.TIER3 in tiers:
            reddit_articles = await ingest_reddit(limit_per_sub=25)
            all_articles.extend(reddit_articles)
            print(f"  Tier-3 Reddit: {len(reddit_articles)}")

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

        # Deduplicate using pure function (also dedupes within batch)
        new_articles, url_dup, content_dup = dedupe_articles(
            all_articles, existing_url_hashes, existing_content_hashes
        )

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

        # Persist new raw articles
        for art in new_articles:
            session.add(art)
        await session.commit()
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

            # Exit guard: zero fetched articles is a real problem (all dupes is fine)
            total_fetched = results["phases"].get("ingestion", {}).get("total_fetched")
            if total_fetched is not None and total_fetched == 0:
                print("\nERROR: total_fetched == 0 (no articles fetched from any source)")
                sys.exit(1)

            # Warn per tier-1 source that produced zero ok articles
            stats_snapshot = results["phases"].get("ingestion", {}).get("extraction_stats", {})
            from src.ingestion.rss import TIER1_FEEDS
            for source_key in TIER1_FEEDS:
                ok_count = stats_snapshot.get(f"{source_key}.ok", 0)
                if ok_count == 0:
                    print(f"::warning title=Source produced no articles::{source_key}")

    except Exception as e:
        print(f"\nPipeline failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())