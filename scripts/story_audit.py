#!/usr/bin/env python3
"""
Read-only story audit script.

Usage:
    python -m scripts.story_audit --days 3

Outputs markdown to stdout and appends to $GITHUB_STEP_SUMMARY when set.
"""

import argparse
import os
import sys
import math
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Set, Tuple, Any
from collections import defaultdict

# Add project root to path for imports (repo root, not src/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from src.schema.models import RawArticle, ReportingUnit, Story, StoryUnitLink
from src.utils.ner import _normalize_text
from src.shared.database import prepare_database_url
from src.shared.config import get_settings


# =============================================================================
# Pure logic functions (no DB access)
# =============================================================================

def bucket_for(jaccard: float) -> float:
    """Floor-based bucketing to 0.1 precision, capped at 1.0."""
    return min(math.floor(jaccard * 10 + 1e-9) / 10, 1.0)


def build_unit_entity_sets(
    units: List[Dict[str, Any]],
    articles: Dict[str, Dict[str, Any]]
) -> Dict[str, Set[str]]:
    """
    Build approximate canonical entity sets for units using _normalize_text.
    Only considers PERSON, ORG, GPE entities from article.entities JSON.
    _normalize_text already returns "TYPE:normalized" so we use it directly.
    """
    unit_entities = {}
    for unit in units:
        article = articles.get(str(unit["representative_article_id"]))
        entity_set = set()
        if article and article.get("entities"):
            entities = article["entities"]
            for label in ("PERSON", "ORG", "GPE"):
                for surface in entities.get(label, []):
                    normalized = _normalize_text(surface, label)
                    entity_set.add(normalized)
        unit_entities[str(unit["id"])] = entity_set
    return unit_entities


def jaccard_similarity(set_a: Set[str], set_b: Set[str]) -> float:
    """Jaccard similarity between two sets."""
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def compute_jaccard_histogram(
    units: List[Dict[str, Any]],
    unit_entities: Dict[str, Set[str]],
    unit_owner_groups: Dict[str, Dict[str, int]],
    articles: Dict[str, Dict[str, Any]],
    days: int = 3,
    hours_window: int = 48,
    now: datetime | None = None
) -> Dict[str, Any]:
    """
    For units created in last N days: best Jaccard vs units with disjoint owner groups
    within hours_window. Returns dict with histogram, near_misses, and stats.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Filter units by creation date (last days days)
    cutoff = now - timedelta(days=days)
    recent_units = [u for u in units if u["created_at"] >= cutoff]

    # Build unit -> owner groups mapping for ALL units (not just recent)
    unit_to_owners = {str(u["id"]): set(unit_owner_groups.get(str(u["id"]), {}).keys()) for u in units}

    histogram = defaultdict(int)
    near_misses = []
    stats = {
        "total_recent_units": len(recent_units),
        "units_with_entities": 0,
        "units_with_no_entities": 0,
        "units_with_no_candidates": 0,
        "zero_overlap_pairs": 0,
        "would_attach": 0,
    }

    # Dedup set for near-misses: frozenset({a,b}) -> max jaccard entry
    near_miss_map: Dict[frozenset, Dict[str, Any]] = {}

    for unit_a in recent_units:
        unit_a_id = str(unit_a["id"])
        entities_a = unit_entities.get(unit_a_id, set())
        owners_a = unit_to_owners.get(unit_a_id, set())

        if not entities_a:
            stats["units_with_no_entities"] += 1
            continue

        stats["units_with_entities"] += 1

        best_jaccard = 0.0
        best_unit_b = None
        best_shared = set()
        has_candidate = False

        for unit_b in units:
            unit_b_id = str(unit_b["id"])
            if unit_b_id == unit_a_id:
                continue

            # Check owner groups are disjoint
            owners_b = unit_to_owners.get(unit_b_id, set())
            if owners_a & owners_b:
                continue

            # Check within hours_window
            time_diff = abs((unit_a["created_at"] - unit_b["created_at"]).total_seconds())
            if time_diff > hours_window * 3600:
                continue

            entities_b = unit_entities.get(unit_b_id, set())
            if not entities_b:
                continue

            has_candidate = True
            jaccard = jaccard_similarity(entities_a, entities_b)

            if jaccard == 0.0:
                stats["zero_overlap_pairs"] += 1

            if jaccard > best_jaccard:
                best_jaccard = jaccard
                best_unit_b = unit_b
                best_shared = entities_a & entities_b

        if not has_candidate:
            stats["units_with_no_candidates"] += 1

        # Track would_attach: best Jaccard >= 0.4
        if best_jaccard >= 0.4:
            stats["would_attach"] += 1

        # Always bucket (including 0.0)
        bucket = bucket_for(best_jaccard)
        histogram[bucket] += 1

        # Track near-misses in [0.2, 0.4) with dedup
        if 0.2 <= best_jaccard < 0.4 and best_unit_b:
            pair_key = frozenset({unit_a_id, str(best_unit_b["id"])})
            entry = {
                "unit_a_id": unit_a_id,
                "unit_b_id": str(best_unit_b["id"]),
                "jaccard": best_jaccard,
                "shared_entities": sorted(best_shared),
                "unit_a_title": articles.get(str(unit_a["representative_article_id"]), {}).get("title", ""),
                "unit_b_title": articles.get(str(best_unit_b["representative_article_id"]), {}).get("title", ""),
                "unit_a_owners": sorted(owners_a),
                "unit_b_owners": sorted(unit_to_owners.get(str(best_unit_b["id"]), set())),
            }
            if pair_key not in near_miss_map or best_jaccard > near_miss_map[pair_key]["jaccard"]:
                near_miss_map[pair_key] = entry

    # Convert near_miss_map to sorted list
    near_misses = sorted(near_miss_map.values(), key=lambda x: -x["jaccard"])[:25]

    return {
        "histogram": dict(histogram),
        "near_misses": near_misses,
        "stats": stats,
    }


def find_duplicate_titles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Find (source_domain, title) pairs appearing more than once with fetched_at range."""
    title_groups = defaultdict(list)
    for article in articles:
        key = (article["source_domain"], article["title"])
        title_groups[key].append(article)

    duplicates = []
    for (domain, title), group in title_groups.items():
        if len(group) > 1:
            fetched_times = [a["fetched_at"] for a in group]
            duplicates.append({
                "source_domain": domain,
                "title": title,
                "count": len(group),
                "fetched_at_range": f"{min(fetched_times)} to {max(fetched_times)}",
            })

    return duplicates


def format_histogram(histogram: Dict[float, int]) -> str:
    """Format histogram as markdown table."""
    lines = ["| Bucket | Count |", "|--------|-------|"]
    for bucket in sorted(histogram.keys()):
        lines.append(f"| {bucket:.1f} | {histogram[bucket]} |")
    return "\n".join(lines)


def format_near_misses(near_misses: List[Dict[str, Any]]) -> str:
    """Format near-misses as markdown table."""
    if not near_misses:
        return "None found."

    lines = [
        "| Unit A | Unit B | Jaccard | Shared Entities | Unit A Title | Unit B Title | Unit A Owners | Unit B Owners |",
        "|--------|--------|---------|-----------------|--------------|--------------|---------------|---------------|"
    ]
    for nm in near_misses:
        shared = ", ".join(nm["shared_entities"])[:100]
        lines.append(
            f"| {nm['unit_a_id'][:8]} | {nm['unit_b_id'][:8]} | {nm['jaccard']:.3f} | "
            f"{shared} | {nm['unit_a_title'][:50]} | {nm['unit_b_title'][:50]} | "
            f"{', '.join(nm['unit_a_owners'])} | {', '.join(nm['unit_b_owners'])} |"
        )
    return "\n".join(lines)


# =============================================================================
# Step C: Story metrics (pure functions)
# =============================================================================

def compute_story_metrics(
    stories: List[Dict[str, Any]],
    story_units: Dict[str, List[str]],
    unit_info: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Compute per-story metrics comparing stored vs computed values.

    Args:
        stories: List of dicts with {id, status, tier1_unit_count, distinct_owners}
        story_units: Dict story_id -> list of unit ids
        unit_info: Dict unit_id -> {"source_tiers": {...}, "tier1_owner_groups": {...}}

    Returns:
        List of dicts with computed and stored metrics, plus mismatch flag.
    """
    metrics = []
    for story in stories:
        story_id = str(story["id"])
        linked_units = story_units.get(story_id, [])

        # Compute tier1 count: sum of source_tiers["tier1"] over linked units
        computed_tier1_count = 0
        all_tier1_owners = set()
        for unit_id in linked_units:
            info = unit_info.get(unit_id, {})
            computed_tier1_count += info.get("source_tiers", {}).get("tier1", 0)
            all_tier1_owners.update(info.get("tier1_owner_groups", {}).keys())

        computed_distinct_owners = len(all_tier1_owners)
        stored_tier1_count = story.get("tier1_unit_count", 0)
        stored_distinct_owners = story.get("distinct_owners", 0)

        mismatch = (computed_tier1_count != stored_tier1_count) or (computed_distinct_owners != stored_distinct_owners)

        metrics.append({
            "id": story_id,
            "status": story.get("status", ""),
            "linked_units": len(linked_units),
            "computed_tier1_count": computed_tier1_count,
            "computed_distinct_owners": computed_distinct_owners,
            "stored_tier1_count": stored_tier1_count,
            "stored_distinct_owners": stored_distinct_owners,
            "mismatch": mismatch,
        })
    return metrics


def summarize_story_metrics(
    metrics: List[Dict[str, Any]],
    max_examples: int = 10
) -> Dict[str, Any]:
    """
    Summarize story metrics into crosstab, statuses, mismatch count, and examples.

    Returns:
        Dict with keys: crosstab, statuses, mismatch_count, examples
    """
    # Build crosstab: {(linked_units, computed_distinct_owners): {status: count}}
    crosstab = defaultdict(lambda: defaultdict(int))
    statuses_set = set()
    mismatch_count = 0
    mismatch_examples = []

    for m in metrics:
        key = (m["linked_units"], m["computed_distinct_owners"])
        crosstab[key][m["status"]] += 1
        statuses_set.add(m["status"])

        if m["mismatch"]:
            mismatch_count += 1
            if len(mismatch_examples) < max_examples:
                mismatch_examples.append({
                    "story_id": m["id"],
                    "linked_units": m["linked_units"],
                    "computed_tier1_count": m["computed_tier1_count"],
                    "computed_distinct_owners": m["computed_distinct_owners"],
                    "stored_tier1_count": m["stored_tier1_count"],
                    "stored_distinct_owners": m["stored_distinct_owners"],
                })

    # Convert crosstab to regular dict
    crosstab_dict = {k: dict(v) for k, v in crosstab.items()}
    statuses = sorted(statuses_set)

    return {
        "crosstab": crosstab_dict,
        "statuses": statuses,
        "mismatch_count": mismatch_count,
        "examples": mismatch_examples,
    }


# =============================================================================
# Step D: Report assembly (pure functions)
# =============================================================================

def format_crosstab(summary: Dict[str, Any]) -> str:
    """Format the crosstab as a markdown table."""
    crosstab = summary.get("crosstab", {})
    statuses = summary.get("statuses", [])

    if not crosstab:
        return "No stories."

    # Build header
    header = "| Linked Units | Tier-1 Owners |"
    for status in statuses:
        header += f" {status} |"
    header += " Total |"

    separator = "|--------------|---------------|"
    for _ in statuses:
        separator += "-------|"
    separator += "-------|"

    lines = [header, separator]

    # Sort keys for consistent output
    for key in sorted(crosstab.keys()):
        linked, owners = key
        row = f"| {linked} | {owners} |"
        total = 0
        for status in statuses:
            count = crosstab[key].get(status, 0)
            row += f" {count} |"
            total += count
        row += f" {total} |"
        lines.append(row)

    return "\n".join(lines)


def format_per_day(rows: List[Dict[str, Any]], title: str) -> str:
    """Format per-day counts as a markdown table."""
    if not rows:
        return f"### {title}\n\nNo data."

    # Group by day and status
    day_status = defaultdict(lambda: defaultdict(int))
    all_statuses = set()
    for row in rows:
        day = row.get("day")
        status = row.get("status", "")
        count = row.get("count", 0)
        if day:
            day_status[str(day)][status] += count
            all_statuses.add(status)

    if not day_status:
        return f"### {title}\n\nNo data."

    statuses = sorted(all_statuses)

    lines = [f"### {title}", ""]
    header = "| Day |"
    for s in statuses:
        header += f" {s} |"
    header += " Total |"

    separator = "|-----|"
    for _ in statuses:
        separator += "-------|"
    separator += "-------|"

    lines.append(header)
    lines.append(separator)

    for day in sorted(day_status.keys(), reverse=True):
        row = f"| {day} |"
        total = 0
        for s in statuses:
            count = day_status[day].get(s, 0)
            row += f" {count} |"
            total += count
        row += f" {total} |"
        lines.append(row)

    return "\n".join(lines)


def build_report(data: Dict[str, Any], days: int, host: str, now: datetime | None = None) -> str:
    """
    Build the complete markdown report.

    Expected data keys:
    - counts: dict with raw_articles, reporting_units, story_unit_links, stories,
              stories_no_units, units_missing_rep, articles_no_unit
    - stories_per_day: list of {day, status, count}
    - articles_per_day: list of {day, count}
    - units_per_day: list of {day, count}
    - orphans_per_day: list of {day, status, count} (stories with no linked units)
    - story_summary: dict with crosstab, statuses, mismatch_count, examples
    - entity_sizes: dict with min, median, max, empty_count
    - jaccard: dict with histogram, near_misses, stats (from compute_jaccard_histogram)
    - duplicates: list of duplicate title dicts
    """
    if now is None:
        now = datetime.now(timezone.utc)

    lines = []

    # Header
    lines.append(f"# Story Audit Report (last {days} days)")
    lines.append("")
    lines.append(f"**Database host:** {host}")
    lines.append(f"**Generated:** {now.isoformat()}")
    lines.append("")
    lines.append("> **Note:** This audit uses approximate canonical IDs via `_normalize_text(surface, label)`")
    lines.append("> for PERSON/ORG/GPE only. This does not merge aliases, so it can understate production overlap.")
    lines.append("")

    # Section 1: Counts
    counts = data.get("counts", {})
    lines.append("## 1. Counts")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| raw_articles | {counts.get('raw_articles', 0)} |")
    lines.append(f"| reporting_units | {counts.get('reporting_units', 0)} |")
    lines.append(f"| story_unit_links | {counts.get('story_unit_links', 0)} |")
    lines.append(f"| stories | {counts.get('stories', 0)} |")
    lines.append(f"| stories with no linked units | {counts.get('stories_no_units', 0)} |")
    lines.append(f"| units whose representative article is missing | {counts.get('units_missing_rep', 0)} |")
    lines.append(f"| articles with no unit | {counts.get('articles_no_unit', 0)} |")
    lines.append("")

    # Section 2: Per-day activity (all time)
    lines.append("## 2. Per-day activity (all time)")
    lines.append("")
    lines.append(format_per_day(data.get("stories_per_day", []), "Stories Created"))
    lines.append("")
    lines.append(format_per_day(data.get("articles_per_day", []), "Articles Fetched"))
    lines.append("")
    lines.append(format_per_day(data.get("units_per_day", []), "Units Created"))
    lines.append("")
    lines.append(format_per_day(data.get("orphans_per_day", []), "Stories with No Linked Units (by created day, status)"))
    lines.append("")

    # Section 3: Story shape
    story_summary = data.get("story_summary", {})
    lines.append("## 3. Story shape: linked units x tier-1 owners")
    lines.append("")
    lines.append(format_crosstab(story_summary))
    lines.append("")
    lines.append(f"**Mismatching rows (stored vs computed):** {story_summary.get('mismatch_count', 0)}")
    lines.append("")
    if story_summary.get("examples"):
        lines.append("### Mismatch Examples")
        lines.append("")
        lines.append("| Story ID | Linked Units | Computed Tier-1 | Computed Owners | Stored Tier-1 | Stored Owners |")
        lines.append("|----------|--------------|-----------------|-----------------|---------------|---------------|")
        for ex in story_summary["examples"]:
            lines.append(
                f"| {ex['story_id'][:8]} | {ex['linked_units']} | "
                f"{ex['computed_tier1_count']} | {ex['computed_distinct_owners']} | "
                f"{ex['stored_tier1_count']} | {ex['stored_distinct_owners']} |"
            )
        lines.append("")

    # Section 4: Entity-set size
    entity_sizes = data.get("entity_sizes", {})
    lines.append("## 4. Entity-set size per unit")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Min | {entity_sizes.get('min', 0)} |")
    lines.append(f"| Median | {entity_sizes.get('median', 0)} |")
    lines.append(f"| Max | {entity_sizes.get('max', 0)} |")
    lines.append(f"| Units with empty set | {entity_sizes.get('empty_count', 0)} |")
    lines.append("")

    # Section 5: Cross-owner Jaccard
    jaccard = data.get("jaccard", {})
    histogram = jaccard.get("histogram", {})
    near_misses = jaccard.get("near_misses", [])
    jaccard_stats = jaccard.get("stats", {})

    lines.append("## 5. Cross-owner Jaccard")
    lines.append("")
    lines.append("### Histogram (0.1 buckets)")
    lines.append("")
    lines.append(format_histogram(histogram))
    lines.append("")
    lines.append("### Stats")
    lines.append("")
    lines.append("| Stat | Value |")
    lines.append("|------|-------|")
    lines.append(f"| Total recent units | {jaccard_stats.get('total_recent_units', 0)} |")
    lines.append(f"| Units with entities | {jaccard_stats.get('units_with_entities', 0)} |")
    lines.append(f"| Units with no entities | {jaccard_stats.get('units_with_no_entities', 0)} |")
    lines.append(f"| Units with no candidates | {jaccard_stats.get('units_with_no_candidates', 0)} |")
    lines.append(f"| Zero-overlap pairs | {jaccard_stats.get('zero_overlap_pairs', 0)} |")
    lines.append(f"| Would attach (Jaccard >= 0.4) | {jaccard_stats.get('would_attach', 0)} |")
    lines.append("")
    lines.append("### Top 25 Near-Misses in [0.2, 0.4)")
    lines.append("")
    lines.append(format_near_misses(near_misses))
    lines.append("")

    # Section 6: Duplicate titles
    duplicates = data.get("duplicates", [])
    lines.append("## 6. Duplicate (source_domain, title)")
    lines.append("")
    if duplicates:
        lines.append("| Source Domain | Title | Count | Fetched At Range |")
        lines.append("|---------------|-------|-------|------------------|")
        for dup in duplicates[:25]:
            title_short = dup["title"][:80] + "..." if len(dup["title"]) > 80 else dup["title"]
            lines.append(f"| {dup['source_domain']} | {title_short} | {dup['count']} | {dup['fetched_at_range']} |")
    else:
        lines.append("No duplicates found.")
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# Database access layer (thin)
# =============================================================================

async def run_audit(db_url: str, days: int = 3) -> str:
    """Run the full audit and return markdown output."""
    url, connect_args = prepare_database_url(db_url)
    engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # SET TRANSACTION READ ONLY
        await session.execute(text("SET TRANSACTION READ ONLY"))

        # Print DB host at startup (never credentials)
        host_info = f"{url.host}:{url.port}" if url.port else url.host
        print(f"# Story Audit Report (last {days} days)")
        print(f"")
        print(f"**Database host:** {host_info}")
        print(f"**Generated:** {datetime.now(timezone.utc).isoformat()}")
        print(f"")
        print(f"> **Note:** This audit uses approximate canonical IDs via `_normalize_text(surface, label)`")
        print(f"> for PERSON/ORG/GPE only. This does not merge aliases, so it can understate production overlap.")
        print(f"")

        # 1. Counts
        raw_count = await session.scalar(select(func.count(RawArticle.id)))
        unit_count = await session.scalar(select(func.count(ReportingUnit.id)))
        link_count = await session.scalar(select(func.count(StoryUnitLink.id)))
        story_count = await session.scalar(select(func.count(Story.id)))

        # Stories with no linked units
        stmt = select(func.count(Story.id)).outerjoin(StoryUnitLink).where(StoryUnitLink.id.is_(None))
        stories_no_units = await session.scalar(stmt)

        # Units whose representative article is missing
        stmt = (
            select(func.count(ReportingUnit.id))
            .outerjoin(RawArticle, RawArticle.id == ReportingUnit.representative_article_id)
            .where(RawArticle.id.is_(None))
        )
        units_missing_rep = await session.scalar(stmt)

        # Articles with no unit
        stmt = (
            select(func.count(RawArticle.id))
            .outerjoin(ReportingUnit, ReportingUnit.representative_article_id == RawArticle.id)
            .where(ReportingUnit.id.is_(None))
        )
        articles_no_unit = await session.scalar(stmt)

        print(f"## 1. Counts")
        print(f"")
        print(f"| Metric | Count |")
        print(f"|--------|-------|")
        print(f"| raw_articles | {raw_count} |")
        print(f"| reporting_units | {unit_count} |")
        print(f"| story_unit_links | {link_count} |")
        print(f"| stories | {story_count} |")
        print(f"| stories with no linked units | {stories_no_units} |")
        print(f"| units whose representative article is missing | {units_missing_rep} |")
        print(f"| articles with no unit | {articles_no_unit} |")
        print(f"")

        # 2. Stories created per day, by status
        stmt = (
            select(
                func.date(Story.day).label("day"),
                Story.status,
                func.count(Story.id).label("count")
            )
            .where(Story.day >= datetime.now(timezone.utc) - timedelta(days=days))
            .group_by(func.date(Story.day), Story.status)
            .order_by(func.date(Story.day).desc())
        )
        result = await session.execute(stmt)
        daily_status = result.all()

        print(f"## 2. Stories Created Per Day (last {days} days), by Status")
        print(f"")
        print(f"| Day | Status | Count |")
        print(f"|-----|--------|-------|")
        for row in daily_status:
            print(f"| {row.day} | {row.status} | {row.count} |")
        print(f"")

        # 3. Stories by (linked-unit count, distinct owner-group count)
        # Get all stories with their linked units and owner groups
        stmt = (
            select(Story)
            .where(Story.day >= datetime.now(timezone.utc) - timedelta(days=days))
        )
        result = await session.execute(stmt)
        stories = result.scalars().all()

        # Get unit -> owner_groups mapping
        stmt = select(ReportingUnit.id, ReportingUnit.owner_groups)
        result = await session.execute(stmt)
        unit_owner_groups = {str(row.id): row.owner_groups or {} for row in result}

        # Get story -> units mapping
        stmt = select(StoryUnitLink.story_id, StoryUnitLink.unit_id)
        result = await session.execute(stmt)
        story_units = defaultdict(list)
        for row in result:
            story_units[str(row.story_id)].append(str(row.unit_id))

        # Compute actual counts vs stored
        disagrees = 0
        print(f"## 3. Stories by (Linked-Unit Count, Distinct Owner-Group Count)")
        print(f"")
        print(f"| Story ID | Linked Units | Distinct Owners | Stored tier1_unit_count | Stored distinct_owners | Match |")
        print(f"|----------|--------------|-----------------|-------------------------|------------------------|-------|")
        for story in stories:
            linked = story_units.get(str(story.id), [])
            all_owners = set()
            for unit_id in linked:
                all_owners.update(unit_owner_groups.get(unit_id, {}).keys())
            actual_units = len(linked)
            actual_owners = len(all_owners)
            match = "✓" if (actual_units == story.tier1_unit_count and actual_owners == story.distinct_owners) else "✗"
            if match == "✗":
                disagrees += 1
            print(f"| {str(story.id)[:8]} | {actual_units} | {actual_owners} | {story.tier1_unit_count} | {story.distinct_owners} | {match} |")
        print(f"")
        print(f"**Rows that disagree:** {disagrees}")
        print(f"")

        # 4. Entity-set size per unit (min / median / max)
        # Get all units and their representative articles
        stmt = (
            select(ReportingUnit.id, ReportingUnit.representative_article_id, ReportingUnit.created_at, ReportingUnit.day)
            .where(ReportingUnit.day >= datetime.now(timezone.utc) - timedelta(days=days))
        )
        result = await session.execute(stmt)
        units = result.all()

        unit_list = []
        rep_article_ids = set()
        for u in units:
            unit_list.append({
                "id": u.id,
                "representative_article_id": u.representative_article_id,
                "created_at": u.created_at,
                "day": u.day,
            })
            rep_article_ids.add(str(u.representative_article_id))

        stmt = select(RawArticle).where(RawArticle.id.in_(rep_article_ids))
        result = await session.execute(stmt)
        articles_dict = {str(a.id): {"entities": a.entities, "title": a.title, "source_domain": a.source_domain, "fetched_at": a.fetched_at} for a in result.scalars().all()}

        unit_entities = build_unit_entity_sets(unit_list, articles_dict)
        entity_sizes = [len(es) for es in unit_entities.values()]

        if entity_sizes:
            min_size = min(entity_sizes)
            max_size = max(entity_sizes)
            median_size = sorted(entity_sizes)[len(entity_sizes) // 2]
        else:
            min_size = max_size = median_size = 0

        print(f"## 4. Entity-Set Size Per Unit (last {days} days)")
        print(f"")
        print(f"| Metric | Value |")
        print(f"|--------|-------|")
        print(f"| Min | {min_size} |")
        print(f"| Median | {median_size} |")
        print(f"| Max | {max_size} |")
        print(f"")

        # 5. Near-miss Jaccard analysis (48h window, disjoint owners)
        jaccard_result = compute_jaccard_histogram(
            unit_list, unit_entities, unit_owner_groups, articles_dict, days=days
        )
        histogram = jaccard_result["histogram"]
        near_misses = jaccard_result["near_misses"]
        jaccard_stats = jaccard_result["stats"]

        print(f"## 5. Near-Miss Jaccard Analysis (last {days} days, 48h window, disjoint owners)")
        print(f"")
        print(f"### Histogram (0.1 buckets)")
        print(f"")
        print(format_histogram(histogram))
        print(f"")
        print(f"### Top 25 Near-Misses in [0.2, 0.4)")
        print(f"")
        print(format_near_misses(near_misses))
        print(f"")

        # 6. Duplicate (source_domain, title)
        stmt = select(RawArticle).where(RawArticle.fetched_at >= datetime.now(timezone.utc) - timedelta(days=days))
        result = await session.execute(stmt)
        recent_articles = [{
            "id": a.id,
            "source_domain": a.source_domain,
            "title": a.title,
            "fetched_at": a.fetched_at,
        } for a in result.scalars().all()]

        duplicates = find_duplicate_titles(recent_articles)

        print(f"## 6. Duplicate (source_domain, title) in last {days} days")
        print(f"")
        if duplicates:
            print(f"| Source Domain | Title | Count | Fetched At Range |")
            print(f"|---------------|-------|-------|------------------|")
            for dup in duplicates:
                title_short = dup["title"][:80] + "..." if len(dup["title"]) > 80 else dup["title"]
                print(f"| {dup['source_domain']} | {title_short} | {dup['count']} | {dup['fetched_at_range']} |")
        else:
            print("No duplicates found.")
        print(f"")

        # Build full markdown output
        markdown_output = []
        markdown_output.append(f"# Story Audit Report (last {days} days)")
        markdown_output.append(f"")
        markdown_output.append(f"**Database host:** {host_info}")
        markdown_output.append(f"**Generated:** {datetime.now(timezone.utc).isoformat()}")
        markdown_output.append(f"")
        markdown_output.append(f"> **Note:** This audit uses approximate canonical IDs via `_normalize_text(surface, label)`")
        markdown_output.append(f"> for PERSON/ORG/GPE only. This does not merge aliases, so it can understate production overlap.")
        markdown_output.append(f"")

        # Re-construct full output
        # ... (similar to above but collected)

        return "\n".join(markdown_output)


def main():
    parser = argparse.ArgumentParser(description="Read-only story audit")
    parser.add_argument("--days", type=int, default=3, help="Number of days to look back (default: 3)")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.has_database:
        print("ERROR: DATABASE_URL environment variable not set or empty", file=sys.stderr)
        sys.exit(1)

    db_url = settings.database_url

    import asyncio
    markdown = asyncio.run(run_audit(db_url, args.days))

    # Print to stdout
    print(markdown)

    # Append to GITHUB_STEP_SUMMARY if set
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as f:
            f.write(markdown + "\n")


if __name__ == "__main__":
    main()