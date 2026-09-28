"""Main ingestion pipeline entry point."""

import asyncio
import sys
import os
from datetime import datetime, timezone
from typing import List, Dict, Tuple
from src.ingestion.gdelt import GDELT_TIER1_CRITICAL_DOMAINS
from src.ingestion.source_registry import SourceTier
from src.ingestion.adapters.rss_adapter import RssAdapter
from src.ingestion.adapters.gdelt_adapter import GDELTAdapter
from src.ingestion.adapters.reddit_adapter import RedditAdapter
from src.verification.units import build_reporting_units
from src.verification.stories import build_stories
from src.verification.tiers import apply_dynamic_gate
from src.shared.database import get_session, init_db
from src.schema.models import RawArticle, StatusLog
from src.shared.config import get_settings
from src.utils.ingest_stats import STATS
import json


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


def classify_tier1_sources_broken(stats_snapshot: Dict, tier1_sources: Dict) -> Tuple[List[str], Dict[str, Dict]]:
    """
    Classify each enabled tier-1 source as BROKEN or OK.

    A source is BROKEN if:
    1. entries_in_feed > 0 and ok + already_known == 0 (extraction ran but nothing succeeded)
    2. All feeds failed (feed_failed:* present and no feed_ok)
    3. Feeds OK but zero entries_in_feed while other tier-1 sources have entries_in_feed > 0

    Returns:
        (list of broken source domains, dict with per-source breakdown)
    """
    broken_sources = []
    breakdown = {}

    # First, collect which tier-1 sources have entries_in_feed > 0
    sources_with_entries = set()
    for source_key, source_config in tier1_sources.items():
        entries_in_feed = stats_snapshot.get(f"{source_config.domain}.entries_in_feed", 0)
        if entries_in_feed > 0:
            sources_with_entries.add(source_config.domain)

    for source_key, source_config in tier1_sources.items():
        domain = source_config.domain
        entries_in_feed = stats_snapshot.get(f"{domain}.entries_in_feed", 0)
        entries_seen = stats_snapshot.get(f"{domain}.entries_seen", 0)
        ok_count = stats_snapshot.get(f"{domain}.ok", 0)
        already_known = stats_snapshot.get(f"{domain}.already_known", 0)

        # Count feed failures and successes
        feed_ok = 0
        feed_failed = 0
        for key, value in stats_snapshot.items():
            if key.startswith(f"{domain}.feed_ok"):
                feed_ok += value
            elif key.startswith(f"{domain}.feed_failed"):
                feed_failed += value

        is_broken = False
        reason = ""

        # Condition 1: entries_in_feed > 0 but ok + already_known == 0
        if entries_in_feed > 0 and (ok_count + already_known) == 0:
            is_broken = True
            reason = f"entries_in_feed={entries_in_feed} but ok+already_known=0 (extraction failed for all)"

        # Condition 2: All feeds failed (feed_failed present, no feed_ok)
        elif feed_failed > 0 and feed_ok == 0:
            is_broken = True
            reason = f"all feeds failed (feed_failed={feed_failed}, feed_ok=0)"

        # Condition 3: Feeds OK but zero entries_in_feed while other tier-1 sources have entries_in_feed > 0
        elif feed_ok > 0 and entries_in_feed == 0 and sources_with_entries:
            is_broken = True
            reason = "feeds OK but zero entries_in_feed while other tier-1 sources have entries_in_feed > 0"

        breakdown[domain] = {
            "entries_in_feed": entries_in_feed,
            "entries_seen": entries_seen,
            "ok": ok_count,
            "already_known": already_known,
            "feed_ok": feed_ok,
            "feed_failed": feed_failed,
            "is_broken": is_broken,
            "reason": reason,
        }

        if is_broken:
            broken_sources.append(domain)

    return broken_sources, breakdown


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

        # Build list of adapters for requested tiers
        adapters = []

        # RSS adapters per tier
        if SourceTier.TIER1 in tiers:
            adapters.append(RssAdapter(SourceTier.TIER1))
        if SourceTier.TIER2 in tiers:
            adapters.append(RssAdapter(SourceTier.TIER2))

        # GDELT (only if enabled and tier-1 requested)
        if settings.gdelt_enabled and SourceTier.TIER1 in tiers:
            adapters.append(GDELTAdapter())

        # Reddit (tier-3)
        if SourceTier.TIER3 in tiers:
            adapters.append(RedditAdapter())

        # Fetch from each adapter sequentially (match current sequential ordering
        # to avoid changing GDELT's circuit-breaker timing)
        rss_tier1_count = 0
        rss_tier2_count = 0
        gdelt_count = 0
        reddit_count = 0
        adapter_health = {}

        for adapter in adapters:
            articles = await adapter.fetch()
            all_articles.extend(articles)
            health = await adapter.health_check()
            adapter_health[adapter.name] = health

            # Track counts per source type for backward compatibility
            if adapter.name == "rss_tier1":
                rss_tier1_count = len(articles)
                print(f"  Tier-1 RSS: {rss_tier1_count}")
            elif adapter.name == "rss_tier2":
                rss_tier2_count = len(articles)
                print(f"  Tier-2 RSS: {rss_tier2_count}")
            elif adapter.name == "gdelt":
                gdelt_count = len(articles)
                print(f"  GDELT: {gdelt_count}")
            elif adapter.name == "reddit_tier3":
                reddit_count = len(articles)
                print(f"  Tier-3 Reddit: {reddit_count}")

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

        # Get GDELT health from adapter for tier1_critical_down check
        from src.ingestion.adapter import SourceHealth
        gdelt_health = adapter_health.get("gdelt", SourceHealth(
            status="down",
            detail="GDELT adapter not run",
            succeeded=[],
            failed=[],
            skipped=[],
        ))

        # Check for tier-1 critical GDELT domains down
        tier1_critical_down = sorted(
            GDELT_TIER1_CRITICAL_DOMAINS & set(gdelt_health.failed + gdelt_health.skipped)
        )
        ingest_status = "degraded" if tier1_critical_down else "ok"

        # Add extraction stats to ingestion results
        stats_snapshot = STATS.snapshot()

        results["phases"]["ingestion"] = {
            "rss": rss_tier1_count + rss_tier2_count,
            "gdelt": gdelt_count,
            "reddit": reddit_count,
            "total_fetched": len(all_articles),
            "total_new": len(new_articles),
            "url_duplicates_skipped": url_dup,
            "content_duplicates_skipped": content_dup,
            "gdelt_health": {
                "succeeded": gdelt_health.succeeded,
                "failed": gdelt_health.failed,
                "skipped": gdelt_health.skipped,
            },
            "tier1_critical_down": tier1_critical_down,
            "extraction_stats": stats_snapshot,
            "adapter_health": {
                name: {
                    "status": health.status,
                    "detail": health.detail,
                    "succeeded": health.succeeded,
                    "failed": health.failed,
                    "skipped": health.skipped,
                }
                for name, health in adapter_health.items()
            },
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

        # Phase 4: Apply dynamic gate (re-evaluate modified stories)
        print("Phase 4: Applying dynamic gate...")
        gated = await apply_dynamic_gate(session, story_ids=modified_story_ids)
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

        # Classify tier-1 sources as BROKEN
        if not dry_run:
            stats_snapshot = results["phases"].get("ingestion", {}).get("extraction_stats", {})
            from src.ingestion.source_registry import get_enabled_sources_by_tier
            tier1_sources = get_enabled_sources_by_tier(SourceTier.TIER1)

            broken_sources, breakdown = classify_tier1_sources_broken(stats_snapshot, tier1_sources)
            total_tier1 = len(tier1_sources)
            broken_count = len(broken_sources)

            print("\n--- TIER-1 SOURCE HEALTH ---")
            for domain, info in breakdown.items():
                status = "BROKEN" if info["is_broken"] else "OK"
                print(f"  {domain}: {status} (entries_seen={info['entries_seen']}, ok={info['ok']}, already_known={info['already_known']}, feed_ok={info['feed_ok']}, feed_failed={info['feed_failed']})")
                if info["is_broken"]:
                    print(f"    Reason: {info['reason']}")

            # Exit 1 if >=50% of enabled tier-1 sources are BROKEN, or total tier-1 ok+already_known == 0
            total_tier1_ok_known = sum(
                stats_snapshot.get(f"{s.domain}.ok", 0) + stats_snapshot.get(f"{s.domain}.already_known", 0)
                for s in tier1_sources.values()
            )

            if broken_count * 2 >= total_tier1 or total_tier1_ok_known == 0:
                print(f"\nERROR: {broken_count}/{total_tier1} tier-1 sources BROKEN (>=50% threshold) or total tier-1 ok+already_known=0")
                for domain in broken_sources:
                    print(f"::error title=Tier-1 source BROKEN::{domain}: {breakdown[domain]['reason']}")
                sys.exit(1)
            elif broken_count > 0:
                # Below threshold: print as ::error annotations without failing
                print(f"\nWARNING: {broken_count}/{total_tier1} tier-1 sources BROKEN (below 50% threshold)")
                for domain in broken_sources:
                    print(f"::error title=Tier-1 source BROKEN (below threshold)::{domain}: {breakdown[domain]['reason']}")
            else:
                print(f"\nAll {total_tier1} tier-1 sources OK")

            # Always write per-source stats to GITHUB_STEP_SUMMARY (including tier-2 and Reddit)
            stats_md = STATS.render_markdown()
            if stats_md:
                print("\n--- INGESTION STATS ---")
                print(stats_md)
                summary_path = os.getenv("GITHUB_STEP_SUMMARY")
                if summary_path:
                    with open(summary_path, "a") as f:
                        f.write(f"\n## Ingestion Stats\n\n{stats_md}\n")

            # Exit non-zero if tier-1 critical GDELT domains are down
            tier1_critical_down = results["phases"].get("ingestion", {}).get("tier1_critical_down", [])
            if tier1_critical_down:
                print(f"\nWARNING: tier-1 critical GDELT domains down: {tier1_critical_down}")
                sys.exit(1)

            # Exit guard: zero fetched articles is a real problem
            total_fetched = results["phases"].get("ingestion", {}).get("total_fetched")
            if total_fetched is not None and total_fetched == 0:
                print("\nERROR: total_fetched == 0 (no articles fetched from any source)")
                sys.exit(1)

    except Exception as e:
        print(f"\nPipeline failed: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())