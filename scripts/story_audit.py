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