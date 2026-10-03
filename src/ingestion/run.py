"""Main ingestion pipeline entry point.

All pipeline logic lives here: adapters are run in order, articles are deduped,
persisted, then verified and grouped. scripts/ingest_gdelt_daily.py is only a
scheduler shim that invokes this module, so do not move logic into the script.

Command line options:
  --dry-run          fetch and report, do not persist
  --env dev|prod     select the config profile (see apply_env below)
  --sources a,b      limit the run to these sources, by adapter name or domain
  --tiers n          comma separated SourceTier names to ingest
"""

import argparse
import asyncio
import logging
import sys
import os
from datetime import datetime, timezone
from typing import List, Dict, Tuple
from src.ingestion.adapter import SourceHealth
from src.ingestion.gdelt import GDELT_TIER1_CRITICAL_DOMAINS
from src.ingestion.source_registry import SourceTier, get_enabled_sensor_feeds
from src.ingestion.adapters.rss_adapter import RssAdapter
from src.ingestion.adapters.gdelt_adapter import GDELTAdapter
from src.ingestion.adapters.reddit_adapter import RedditAdapter
from src.ingestion.adapters.sensor_adapter import SensorAdapter
from src.ingestion.adapters.rss_evidence_adapter import RssEvidenceAdapter
# Safe to import at module level precisely because rss_evidence.py no longer
# imports src.transparency.log at module scope; this symbol carries no
# transparency dependency of its own.
from src.ingestion.rss_evidence import TransparencyUnavailableError
from src.verification.units import build_reporting_units
from src.verification.stories import build_stories
from src.verification.tiers import apply_dynamic_gate
from src.shared.bulk_write import bulk_write
from src.shared.database import get_session
from src.schema.models import RawArticle, StatusLog
from src.shared.config import get_settings
from src.utils.ingest_stats import STATS
from src.ingestion.feed_health import (
    DEAD_AFTER_EMPTY_POLLS,
    load_active,
    report_dead_feeds,
    save_active,
)
import json


logger = logging.getLogger(__name__)


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


ENV_PROFILES = ("dev", "prod")

# Dev-only fetch tuning (Tyler-approved). Dev ingests crawl through the egress
# proxy, so at the production concurrency they crawl: a wider semaphore and a
# shorter per-feed timeout keep a dev run bounded and quick. Production values
# stay 10 / 30s and are never touched by this table.
DEV_ENV_DEFAULTS = {
    "RSS_FETCH_CONCURRENCY": "25",
    "RSS_FETCH_TIMEOUT": "15",
}


def apply_env(env: str) -> str:
    """Select the config profile for a run.

    pydantic-settings reads .env by default. For dev we load .env.dev when it
    exists, on top of .env, so a local run can point at a local database. For
    prod we use the environment only, which is what CI provides. Dev also gets
    DEV_ENV_DEFAULTS, applied last so an explicit value from the process
    environment, .env, or .env.dev always wins. Returns the profile name.
    """
    if env not in ENV_PROFILES:
        raise ValueError(f"unknown env {env!r}, expected one of {ENV_PROFILES}")

    os.environ["PIPELINE_ENV"] = env

    try:
        from dotenv import load_dotenv
    except ImportError:
        return env

    load_dotenv(".env", override=False)
    if env == "dev":
        if os.path.exists(".env.dev"):
            load_dotenv(".env.dev", override=True)
            print("Config profile: dev (loaded .env.dev)")
        else:
            print("Config profile: dev (no .env.dev, using .env)")
        applied = [k for k, v in DEV_ENV_DEFAULTS.items() if not os.environ.get(k)]
        for key, value in DEV_ENV_DEFAULTS.items():
            os.environ.setdefault(key, value)
        if applied:
            applied_text = ", ".join(f"{k}={DEV_ENV_DEFAULTS[k]}" for k in applied)
            print(f"Config profile: dev (fetch tuning defaults applied: {applied_text})")
    else:
        print("Config profile: prod (environment only)")

    # get_settings is lru_cached, and the profile is loaded after first import.
    get_settings.cache_clear()
    return env


def build_adapters(
    settings,
    tiers: list[SourceTier],
    sources: list[str] | None = None,
) -> list:
    """Assemble adapters for the requested tiers, optionally filtered by --sources.

    A --sources entry matches an adapter name (rss_tier1, rss_tier2, gdelt,
    reddit_tier3, sensors, rss_evidence) or a registry domain. Unmatched entries
    are reported, not fatal, so a typo shows up in the log instead of silently
    shrinking the run.

    RssEvidenceAdapter is opt-in rather than default: `--sources rss_evidence`
    selects it regardless of --tiers, because it owns its own hand-verified
    feed list and its own polling cadence and is not a tiered source at all.
    It is deliberately absent from the unfiltered candidate list, so a run with
    no --sources behaves exactly as it did before the evidence locker existed.

    The opt-in stays, but it is no longer the whole story, and that is a
    correction worth reading before touching it again. The evidence locker is
    the only path that appends to merkle_log_entries, so leaving it opt-in with
    nothing scheduled asking for it meant the log never grew in production
    (measured on dev 2026-10-02: 1811 articles fetched, 0 stamped) and every
    /proof permalink rendered "pending" forever. It is now run on its own
    schedule by .github/workflows/transparency-stamp.yml, which passes
    `--sources rss_evidence` explicitly and lands before both the 06:00
    checkpoint cron and the 06:23 tiered ingest. tests/test_scheduled_stamping.py
    fails if that workflow stops asking for the adapter.

    Do not "fix" this by adding the locker to the candidate list. The evidence
    feeds are a subset of the tier-1 feeds (src/ingestion/source_registry.py)
    and raw_articles.url_hash is UNIQUE, so in one combined run the tiered
    adapters would claim the rows first and the locker would stamp nothing,
    while doubling the outbound feed fetches.
    """
    candidates = []
    if SourceTier.TIER1 in tiers:
        candidates.append(RssAdapter(SourceTier.TIER1))
    if SourceTier.TIER2 in tiers:
        candidates.append(RssAdapter(SourceTier.TIER2))
    if settings.gdelt_enabled and SourceTier.TIER1 in tiers:
        candidates.append(GDELTAdapter())
    if SourceTier.TIER3 in tiers:
        candidates.append(RedditAdapter())
    if SourceTier.TIER3 in tiers:
        candidates.append(SensorAdapter(list(get_enabled_sensor_feeds().values())))

    if not sources:
        return candidates

    wanted = {s.strip().lower() for s in sources if s.strip()}
    if not wanted:
        return candidates

    # Selected by name only, never by tier, so it is offered separately rather
    # than appended to candidates where --tiers would gate it.
    opt_in = [RssEvidenceAdapter()]
    candidates = candidates + opt_in

    selected = []
    matched_entries = set()
    for adapter in candidates:
        if adapter.name.lower() in wanted:
            selected.append(adapter)
            matched_entries.add(adapter.name.lower())
            continue
        # SensorAdapter reports per feed, so accept the sensor domain names too.
        if adapter.name == "sensors" and (
            {d for d in get_enabled_sensor_feeds()} & wanted
        ):
            selected.append(adapter)
            matched_entries |= {d for d in get_enabled_sensor_feeds()} & wanted
            continue
        if isinstance(adapter, RssAdapter):
            from src.ingestion.source_registry import get_enabled_sources_by_tier
            domains = set(get_enabled_sources_by_tier(adapter.tier))
            hit = domains & wanted
            if hit:
                # Narrow the adapter to just the requested domains. Selecting
                # the whole tier adapter for one named domain made
                # `--sources allafrica.com` poll every tier-2 feed, which reads
                # as a filtered run in the log but is not one.
                selected.append(adapter if hit == domains else RssAdapter(adapter.tier, domains=hit))
                matched_entries |= hit
                continue

    # An unmatched --sources entry is only a mystery if it names nothing at all.
    # A name the registry knows but has enabled=False is a different failure: the
    # operator asked for a real source and will get silence, so say which it was
    # and why it is off rather than reporting it as unknown.
    from src.ingestion.source_registry import ALL_SOURCES

    known_disabled = {d for d, cfg in ALL_SOURCES.items() if not cfg.enabled}
    for entry in sorted(wanted - matched_entries):
        if entry in known_disabled:
            print(
                f"WARNING: --sources entry {entry!r} is a known source but is "
                f"disabled in the registry; it will not be polled"
            )
        else:
            print(f"WARNING: --sources entry {entry!r} matched no adapter")

    return selected


async def run_ingestion(
    dry_run: bool = False,
    tiers: list[SourceTier] | None = None,
    sources: list[str] | None = None,
) -> dict:
    """Run the full ingestion → verification → grouping pipeline.

    Args:
        dry_run: If True, don't persist to database
        tiers: If specified, only ingest these tiers. Default: all tiers.
        sources: If specified, only run these adapters/domains.
    """
    settings = get_settings()
    STATS.reset()
    # Load the cross-run feed health so the empty-streak counters survive restarts,
    # and so a feed that died yesterday is still reported dead today.
    feed_health = load_active()
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
        adapters = build_adapters(settings, tiers, sources)
        print(f"  Adapters: {', '.join(a.name for a in adapters) or 'none'}")

        # Fetch from each adapter sequentially (match current sequential ordering
        # to avoid changing GDELT's circuit-breaker timing)
        rss_tier1_count = 0
        rss_tier2_count = 0
        gdelt_count = 0
        reddit_count = 0
        sensor_count = 0
        rss_evidence_count = 0
        adapter_health = {}

        for adapter in adapters:
            # I-P1-3: one adapter raising must not destroy the whole run.
            # The adapter contract (adapter.py) explicitly permits fetch()
            # to raise on total adapter failure; contain it, record it in
            # adapter_health, and keep the articles already fetched.
            try:
                articles = await adapter.fetch()
            except TransparencyUnavailableError:
                # The evidence locker's integrity dependency is unavailable.
                # NOT contained, unlike every other failure below: this is not
                # "the publisher was unreachable", it is "the subsystem that
                # makes these observations trustworthy is missing". Containing
                # it would exit 0 with an empty Merkle log that every reader
                # would take as a full one -- the worst outcome available to a
                # tamper-evident subsystem. The message names the module.
                logger.exception("Adapter %s cannot run without transparency", adapter.name)
                print(
                    f"  {adapter.name}: FATAL -- the transparency subsystem is "
                    f"unavailable, so nothing can be stamped. Refusing to report "
                    f"a successful run with an empty transparency log."
                )
                raise
            except Exception as e:
                logger.exception(
                    "Adapter %s raised during fetch; continuing with remaining adapters",
                    adapter.name,
                )
                adapter_health[adapter.name] = SourceHealth(
                    status="down",
                    detail=f"fetch() raised {type(e).__name__}: {e}",
                )
                STATS.record(f"adapter.{adapter.name}", "fetch_raised")
                print(f"  {adapter.name}: FAILED ({type(e).__name__}: {e}); continuing with remaining adapters")
                continue
            all_articles.extend(articles)
            try:
                health = await adapter.health_check()
            except Exception as e:
                logger.exception("Adapter %s health_check raised", adapter.name)
                health = SourceHealth(
                    status="degraded",
                    detail=f"health_check() raised {type(e).__name__}: {e}",
                )
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
            elif adapter.name == "sensors":
                sensor_count = len(articles)
                print(f"  Sensors: {sensor_count}")
            elif adapter.name == "rss_evidence":
                rss_evidence_count = len(articles)
                print(f"  RSS evidence: {rss_evidence_count}")

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

        # Phase 1.5: Translate new articles to English (language detect +
        # translated headline/body stored alongside the untouched originals).
        # Translation never breaks ingest: failures are caught inside the
        # module and articles persist untranslated.
        print("Phase 1.5: Translating new articles to English...")
        translation_summary = {"backend": "skipped", "total": 0}
        if new_articles:
            try:
                from src.enrichment.translation import translate_articles

                # In a worker thread: the backend blocks on urllib and a one-second
                # politeness sleep per chunk, and its budget reservation is a sync
                # database call. Running it on the event loop would stall the pipeline
                # for the length of a whole translation batch.
                translation_summary = await asyncio.to_thread(
                    translate_articles, new_articles
                )
                print(
                    f"  Translated: {translation_summary['translated']}, "
                    f"English: {translation_summary['english']}, "
                    f"failed: {translation_summary['failed']} "
                    f"(backend: {translation_summary['backend']})"
                )
            except Exception as exc:
                print(f"  Translation step failed, continuing untranslated: {exc}")
                translation_summary = {"backend": "error", "total": len(new_articles), "error": str(exc)}

        # Phase 1.6: Re-extract entities from the translated English text.
        # Ingestion extracts entities from the ORIGINAL body, and the extractor
        # is an English model, so a non-English article arrived with no entities
        # and stayed inert downstream no matter how well it translated: an
        # entity-less unit builds a story with empty primary_entities, which
        # build_stories() then skips when matching later units, so that story is
        # one unit forever and the corroboration gate can never be evaluated for
        # it. This runs the same extractor over body_text_en so a translated
        # article is actually mergeable and gateable. Same contract as
        # translation: it never breaks ingest.
        print("Phase 1.6: Re-extracting entities from translated text...")
        entity_summary = {"eligible": 0, "refreshed": 0, "entities_found": 0, "errors": 0}
        if new_articles:
            try:
                from src.enrichment.translation import refresh_entities_after_translation

                entity_summary = await asyncio.to_thread(
                    refresh_entities_after_translation,
                    new_articles,
                    settings.top_n_entities,
                )
                print(
                    f"  Translated articles re-extracted: {entity_summary['refreshed']}"
                    f"/{entity_summary['eligible']} eligible, "
                    f"{entity_summary['entities_found']} entities, "
                    f"{entity_summary['errors']} errors"
                )
            except Exception as exc:
                print(f"  Entity refresh failed, keeping original entities: {exc}")
                entity_summary = {"eligible": 0, "refreshed": 0, "entities_found": 0,
                                  "errors": 0, "error": str(exc)}

        # Get GDELT health from adapter for tier1_critical_down check
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

        # Persist what this run saw before anything can return early, and state
        # the dead feeds out loud: a feed that yielded nothing three runs running
        # is a coverage hole, and the only way it stays invisible is if nobody
        # says it. The counters count entries fetched, not articles stored, so a
        # feed whose article pages all 403 does not look healthy.
        feed_health_saved = save_active()
        dead_feeds = report_dead_feeds(feed_health)
        for dead in dead_feeds:
            logger.warning(
                "Feed DEAD (no entries in %d consecutive polls): %s (%s)",
                dead["consecutive_empty"], dead["url"], dead["last_detail"],
            )
        if dead_feeds:
            print(
                f"  Feed health: {len(dead_feeds)} DEAD feed(s) "
                f"(no entries in {DEAD_AFTER_EMPTY_POLLS}+ consecutive polls) - "
                f"see `python scripts/report_feed_health.py`"
            )
            for dead in dead_feeds:
                print(
                    f"    DEAD {dead['url']} "
                    f"[{dead['source_key']}] last: {dead['last_detail']} "
                    f"(empty {dead['consecutive_empty']}x, {dead['polls']} polls total)"
                )
        else:
            print("  Feed health: no dead feeds")

        results["phases"]["ingestion"] = {
            "rss": rss_tier1_count + rss_tier2_count,
            "gdelt": gdelt_count,
            "reddit": reddit_count,
            "sensors": sensor_count,
            "rss_evidence": rss_evidence_count,
            "total_fetched": len(all_articles),
            "total_new": len(new_articles),
            "url_duplicates_skipped": url_dup,
            "content_duplicates_skipped": content_dup,
            "translation": translation_summary,
            "entity_refresh": entity_summary,
            "gdelt_health": {
                "succeeded": gdelt_health.succeeded,
                "failed": gdelt_health.failed,
                "skipped": gdelt_health.skipped,
            },
            "tier1_critical_down": tier1_critical_down,
            "extraction_stats": stats_snapshot,
            "feed_health": {
                "state_saved": feed_health_saved,
                "dead_feeds": dead_feeds,
                "empty_feeds": [
                    {"url": f.url, "source_key": f.source_key,
                     "consecutive_empty": f.consecutive_empty, "last_detail": f.last_detail}
                    for f in sorted(feed_health.empty(), key=lambda x: x.url)
                ],
            },
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

        # Persist new raw articles, chunked: one multi-row INSERT for the whole
        # batch is an all-or-nothing statement, and a run that fetched 997
        # articles lost all of them to a single dropped connection.
        await bulk_write(session, new_articles)
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line options for the pipeline entry point.

    Reads sys.argv by default, like the pre argparse version did with
    "--dry-run" in sys.argv. Unrecognized arguments are reported on stderr and
    then ignored rather than aborting, so calling main() from a process that
    owns a different command line does not kill the run. A mistyped option
    still shows up in the log instead of being silently swallowed.
    """
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="python -m src.ingestion.run",
        description="Run the news ingestion, verification and grouping pipeline.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report without persisting to the database.",
    )
    parser.add_argument(
        "--env",
        choices=list(ENV_PROFILES),
        default=os.getenv("PIPELINE_ENV", "prod"),
        help="Config profile to load: dev reads .env.dev on top of .env, prod uses the environment.",
    )
    parser.add_argument(
        "--sources",
        default="",
        help="Comma separated adapter names (rss_tier1, rss_tier2, gdelt, reddit_tier3, sensors, rss_evidence) or registry domains to limit the run to.",
    )
    parser.add_argument(
        "--tiers",
        default="",
        help="Comma separated SourceTier names (tier1, tier2, tier3, tier4) to ingest.",
    )
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        print(f"WARNING: ignoring unrecognized argument(s): {' '.join(unknown)}", file=sys.stderr)
    return args


def parse_tiers(raw: str) -> list[SourceTier] | None:
    """Turn a --tiers string into SourceTier members, or None for all."""
    names = [n.strip().upper() for n in raw.split(",") if n.strip()]
    if not names:
        return None
    invalid = [n for n in names if n not in {t.name for t in SourceTier}]
    if invalid:
        raise ValueError(f"unknown tier(s): {', '.join(invalid)}")
    return [SourceTier[n] for n in names]


async def main(argv: list[str] | None = None):
    """Main entry point."""
    args = parse_args(argv)
    dry_run = args.dry_run

    apply_env(args.env)

    try:
        results = await run_ingestion(
            dry_run=dry_run,
            tiers=parse_tiers(args.tiers),
            sources=[s for s in args.sources.split(",") if s.strip()],
        )
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

            # I-P1-1: only classify tier-1 domains that were actually polled
            # this run. Stats keys are "<domain>.<counter>" (e.g. "bbc.com.ok");
            # a domain with no keys never ran (e.g. a --sources gdelt filtered
            # run never polls tier-1 RSS), and must not drag the exit guard.
            ran_domains = {
                s.domain
                for s in tier1_sources.values()
                if any(k.startswith(f"{s.domain}.") for k in stats_snapshot)
            }
            ran_sources = {
                key: s for key, s in tier1_sources.items() if s.domain in ran_domains
            }

            print("\n--- TIER-1 SOURCE HEALTH ---")
            if not ran_sources:
                print("  Skipped: no tier-1 RSS domains ran this run (e.g. --sources filtered)")
                broken_sources, breakdown = [], {}
                total_tier1 = 0
                broken_count = 0
                total_tier1_ok_known = 0
            else:
                broken_sources, breakdown = classify_tier1_sources_broken(stats_snapshot, ran_sources)
                total_tier1 = len(ran_sources)
                broken_count = len(broken_sources)

                for domain, info in breakdown.items():
                    status = "BROKEN" if info["is_broken"] else "OK"
                    print(f"  {domain}: {status} (entries_seen={info['entries_seen']}, ok={info['ok']}, already_known={info['already_known']}, feed_ok={info['feed_ok']}, feed_failed={info['feed_failed']})")
                    if info["is_broken"]:
                        print(f"    Reason: {info['reason']}")

                # Exit 1 if >=50% of ran tier-1 sources are BROKEN, or total
                # tier-1 ok+already_known == 0 across the ran domains.
                total_tier1_ok_known = sum(
                    stats_snapshot.get(f"{s.domain}.ok", 0) + stats_snapshot.get(f"{s.domain}.already_known", 0)
                    for s in ran_sources.values()
                )

            # The total==0 arm only fires when at least one ran source is
            # classified broken; otherwise a quiet-but-healthy run exits 1
            # with no signal (and a filtered run with broken_count == 0 skips
            # the branch entirely).
            if broken_count > 0 and (broken_count * 2 >= total_tier1 or total_tier1_ok_known == 0):
                print(f"\nERROR: {broken_count}/{total_tier1} tier-1 sources BROKEN (>=50% threshold) or total tier-1 ok+already_known=0")
                for domain in broken_sources:
                    print(f"::error title=Tier-1 source BROKEN::{domain}: {breakdown[domain]['reason']}")
                sys.exit(1)
            elif broken_count > 0:
                # Below threshold: print as ::error annotations without failing
                print(f"\nWARNING: {broken_count}/{total_tier1} tier-1 sources BROKEN (below 50% threshold)")
                for domain in broken_sources:
                    print(f"::error title=Tier-1 source BROKEN (below threshold)::{domain}: {breakdown[domain]['reason']}")
            elif ran_sources:
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