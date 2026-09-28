#!/usr/bin/env python3
"""Grouping diagnostic report - read-only queries on last 48h of data.

Usage:
    python scripts/grouping_report.py [--hours 48]
"""

import argparse
import asyncio
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from src.schema.models import (
    RawArticle, ReportingUnit, Story, StoryUnitLink, SourceTier,
)
from src.verification.units import get_owner_group


async def run_report(hours: int = 48):
    """Run the grouping diagnostic report."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        # Try to load from .env
        from dotenv import load_dotenv
        load_dotenv()
        database_url = os.getenv("DATABASE_URL")

    if not database_url:
        print("ERROR: DATABASE_URL not found in environment or .env")
        return

    print("Connecting to database...")
    # Supabase pooler uses pgbouncer which doesn't support prepared statements
    engine = create_async_engine(database_url, echo=False, connect_args={"statement_cache_size": 0})
    async_session = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    print(f"Report window: last {hours}h (since {cutoff.isoformat()})")
    print("=" * 80)

    async with async_session() as session:
        # ============================================================
        # 1. Stories touched in last run, split created vs attached-to-existing.
        #    Units linked per story: count of stories with 1, 2, 3+ units.
        # ============================================================
        print("\n[1] STORIES TOUCHED & UNITS PER STORY")
        print("-" * 80)

        # Get stories updated in the last run window
        stmt = select(Story).where(Story.updated_at >= cutoff)
        result = await session.execute(stmt)
        stories = result.scalars().all()

        print(f"Total stories updated in window: {len(stories)}")

        # Count units per story
        units_per_story = Counter()
        created_count = 0
        attached_count = 0

        for story in stories:
            # Count units linked to this story
            stmt = select(func.count(StoryUnitLink.unit_id)).where(StoryUnitLink.story_id == story.id)
            result = await session.execute(stmt)
            unit_count = result.scalar() or 0
            units_per_story[unit_count] += 1

            # Check if story was created in this window (created_at >= cutoff)
            if story.created_at >= cutoff:
                created_count += 1
            else:
                attached_count += 1

        print(f"  Stories created in window: {created_count}")
        print(f"  Stories attached-to-existing: {attached_count}")
        print("  Units per story distribution:")
        for count in sorted(units_per_story.keys()):
            print(f"    {count} unit(s): {units_per_story[count]} stories")

        # ============================================================
        # 2. Stories with >=2 units from distinct tier-1 owner_groups.
        #    The 13 queued: title, backing outlets.
        #    The 209 blocked: count by block reason.
        # ============================================================
        print("\n[2] TIER-1 OWNER GROUP DIVERSITY & GATE RESULTS")
        print("-" * 80)

        # Get stories from the window
        story_ids = [s.id for s in stories]
        if story_ids:
            # Fetch units for these stories
            stmt = (
                select(StoryUnitLink, ReportingUnit)
                .join(ReportingUnit, StoryUnitLink.unit_id == ReportingUnit.id)
                .where(StoryUnitLink.story_id.in_(story_ids))
            )
            result = await session.execute(stmt)
            story_units = result.all()

            # Build story -> tier1 owner groups mapping
            story_tier1_owners = defaultdict(set)
            story_units_detail = defaultdict(list)

            for link, unit in story_units:
                story_units_detail[link.story_id].append(unit)
                if unit.tier1_owner_groups:
                    for owner in unit.tier1_owner_groups.keys():
                        story_tier1_owners[link.story_id].add(owner)

            # Categorize stories
            queued_stories = []
            blocked_stories = []
            blocked_reasons = Counter()

            for story in stories:
                tier1_owners = story_tier1_owners.get(story.id, set())

                # Check story status and gate reason
                if story.status == Story.Status.QUEUED:
                    queued_stories.append((story, tier1_owners))
                elif story.status == Story.Status.BLOCKED:
                    blocked_stories.append(story)
                    if story.gate_reason:
                        blocked_reasons[story.gate_reason] += 1
                    else:
                        blocked_reasons["no_reason_given"] += 1

            print(f"Queued stories (>=2 distinct tier-1 owners): {len(queued_stories)}")
            for story, owners in queued_stories:
                # Get representative article title
                stmt = select(RawArticle).where(RawArticle.id == story.primary_entities[0] if story.primary_entities else None)
                # Better: get a unit's representative article
                stmt = select(ReportingUnit).where(ReportingUnit.id.in_([u.id for u in story_units_detail.get(story.id, [])[:1]]))
                result = await session.execute(stmt)
                unit = result.scalars().first()
                title = "Unknown"
                if unit:
                    stmt = select(RawArticle).where(RawArticle.id == unit.representative_article_id)
                    result = await session.execute(stmt)
                    article = result.scalars().first()
                    if article:
                        title = article.title[:100]
                print(f"  - {title}")
                print(f"    Owner groups: {', '.join(sorted(owners))}")

            print(f"\nBlocked stories: {len(blocked_stories)}")
            print("Blocked by reason:")
            for reason, count in blocked_reasons.most_common():
                # Sanitize reason for console output
                safe_reason = str(reason).encode('ascii', 'replace').decode('ascii')
                print(f"  {safe_reason}: {count}")

        # ============================================================
        # 3. Canonical-entity set size per unit (0, 1-2, 3-5, 6+).
        #    Units with empty sets: count and outlets.
        # ============================================================
        print("\n[3] CANONICAL ENTITY SET SIZES PER UNIT")
        print("-" * 80)

        # Get units from the window
        stmt = select(ReportingUnit).where(ReportingUnit.created_at >= cutoff)
        result = await session.execute(stmt)
        units = result.scalars().all()

        print(f"Total reporting units in window: {len(units)}")

        # Get articles for these units to extract entities
        unit_article_ids = [u.representative_article_id for u in units]
        stmt = select(RawArticle).where(RawArticle.id.in_(unit_article_ids))
        result = await session.execute(stmt)
        articles = {str(a.id): a for a in result.scalars().all()}

        entity_size_buckets = Counter()
        empty_set_outlets = Counter()

        for unit in units:
            article = articles.get(str(unit.representative_article_id))
            entity_count = 0
            if article and article.entities:
                primary_entities = {k: v for k, v in article.entities.items() if k in {"PERSON", "ORG", "GPE"}}
                entity_count = sum(len(v) for v in primary_entities.values())

            if entity_count == 0:
                entity_size_buckets["0 (empty)"] += 1
                empty_set_outlets[article.source_domain if article else "unknown"] += 1
            elif entity_count <= 2:
                entity_size_buckets["1-2"] += 1
            elif entity_count <= 5:
                entity_size_buckets["3-5"] += 1
            else:
                entity_size_buckets["6+"] += 1

        print("Entity set size distribution:")
        for bucket in ["0 (empty)", "1-2", "3-5", "6+"]:
            print(f"  {bucket}: {entity_size_buckets[bucket]} units")

        print(f"\nUnits with empty entity sets: {entity_size_buckets['0 (empty)']}")
        print("Outlets with empty entity sets:")
        for outlet, count in empty_set_outlets.most_common(10):
            print(f"  {outlet}: {count}")

        # ============================================================
        # 4. Replay for likely-same-event pairs from DIFFERENT tier-1 owner groups
        #    with cosine >= 0.80. Report merge rate and Jaccard distribution.
        # ============================================================
        print("\n[4] CROSS-OUTLET SAME-EVENT PAIR REPLAY")
        print("-" * 80)

        # Build unit -> entity set and owner_group mapping for tier-1 units
        tier1_units = []
        for unit in units:
            article = articles.get(str(unit.representative_article_id))
            if article and article.source_tier == SourceTier.TIER1:
                owner = get_owner_group(article.source_domain)
                entity_set = set()
                if article and article.entities:
                    primary_entities = {k: v for k, v in article.entities.items() if k in {"PERSON", "ORG", "GPE"}}
                    # For Jaccard we just use the surface forms (not canonical IDs)
                    for v in primary_entities.values():
                        entity_set.update(v)
                tier1_units.append({
                    "unit_id": unit.id,
                    "article_id": unit.representative_article_id,
                    "title": article.title[:150] if article else "No title",
                    "source_domain": article.source_domain if article else "unknown",
                    "owner_group": owner,
                    "entities": entity_set,
                    "body_text": article.body_text[:500] if article and article.body_text else "",
                })

        print(f"Tier-1 units in window: {len(tier1_units)}")
        print(f"Owner groups represented: {sorted(set(u['owner_group'] for u in tier1_units))}")

        # Compute title similarity (TF-IDF cosine) for cross-outlet pairs
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        if len(tier1_units) >= 2:
            titles = [u["title"] for u in tier1_units]
            vectorizer = TfidfVectorizer(stop_words='english', max_features=1000)
            tfidf_matrix = vectorizer.fit_transform(titles)
            cos_sim = cosine_similarity(tfidf_matrix)

            # Find cross-outlet pairs with cosine >= 0.80
            high_sim_pairs = []
            for i in range(len(tier1_units)):
                for j in range(i + 1, len(tier1_units)):
                    if tier1_units[i]["owner_group"] != tier1_units[j]["owner_group"]:
                        sim = cos_sim[i, j]
                        if sim >= 0.80:
                            # Compute entity Jaccard
                            e1 = tier1_units[i]["entities"]
                            e2 = tier1_units[j]["entities"]
                            if e1 or e2:
                                jaccard = len(e1 & e2) / len(e1 | e2) if (e1 | e2) else 0.0
                            else:
                                jaccard = 0.0
                            high_sim_pairs.append({
                                "pair": (i, j),
                                "title_sim": sim,
                                "entity_jaccard": jaccard,
                                "unit1": tier1_units[i],
                                "unit2": tier1_units[j],
                            })

            print(f"\nCross-outlet pairs with title cosine >= 0.80: {len(high_sim_pairs)}")

            if high_sim_pairs:
                # Check which pairs merged into same story
                merged_count = 0
                not_merged = []

                for pair in high_sim_pairs:
                    u1_id = str(pair["unit1"]["unit_id"])
                    u2_id = str(pair["unit2"]["unit_id"])

                    # Check if they're in the same story
                    stmt = select(StoryUnitLink.story_id).where(StoryUnitLink.unit_id.in_([u1_id, u2_id]))
                    result = await session.execute(stmt)
                    story_ids = [str(r[0]) for r in result.all()]

                    if len(set(story_ids)) == 1 and len(story_ids) == 2:
                        merged_count += 1
                    else:
                        not_merged.append(pair)

                print(f"  Merged into same story: {merged_count}/{len(high_sim_pairs)} ({merged_count/len(high_sim_pairs)*100:.1f}%)")
                print(f"  Did NOT merge: {len(not_merged)}")

                if not_merged:
                    # Jaccard distribution for non-merged
                    jaccard_buckets = Counter()
                    for pair in not_merged:
                        j = pair["entity_jaccard"]
                        if j == 0:
                            jaccard_buckets["0"] += 1
                        elif j < 0.20:
                            jaccard_buckets["0.01-0.19"] += 1
                        elif j < 0.40:
                            jaccard_buckets["0.20-0.39"] += 1
                        else:
                            jaccard_buckets[">=0.40"] += 1

                    print("\n  Entity Jaccard distribution for non-merged pairs:")
                    for bucket in ["0", "0.01-0.19", "0.20-0.39", ">=0.40"]:
                        print(f"    {bucket}: {jaccard_buckets[bucket]}")

                    print("\n  Example non-merged pairs (10):")
                    for i, pair in enumerate(not_merged[:10]):
                        u1 = pair["unit1"]
                        u2 = pair["unit2"]
                        print(f"    {i+1}. Jaccard={pair['entity_jaccard']:.3f}, title_sim={pair['title_sim']:.3f}")
                        def sanitize(s):
                            return str(s).encode('ascii', 'replace').decode('ascii')
                        print(f"       {sanitize(u1['owner_group'])} ({sanitize(u1['source_domain'])}): {sanitize(u1['title'][:80])}")
                        print(f"       {sanitize(u2['owner_group'])} ({sanitize(u2['source_domain'])}): {sanitize(u2['title'][:80])}")
                        print(f"       Entities1: {sorted(sanitize(e) for e in u1['entities'])[:10]}")
                        print(f"       Entities2: {sorted(sanitize(e) for e in u2['entities'])[:10]}")

        # ============================================================
        # 5. Union-growth check for stories with >=3 units
        # ============================================================
        print("\n[5] UNION-GROWTH CHECK")
        print("-" * 80)

        # Get stories with >=3 units from the window
        story_unit_counts = defaultdict(list)
        for link, unit in story_units:
            story_unit_counts[link.story_id].append(unit)

        flip_count = 0
        total_checked = 0

        for story_id, story_units_list in story_unit_counts.items():
            if len(story_units_list) >= 3:
                # Get aggregate entity set for this story
                aggregate_entities = set()
                for unit in story_units_list:
                    article = articles.get(str(unit.representative_article_id))
                    if article and article.entities:
                        primary_entities = {k: v for k, v in article.entities.items() if k in {"PERSON", "ORG", "GPE"}}
                        for v in primary_entities.values():
                            aggregate_entities.update(v)

                # For each unit in the story, compare Jaccard(unit, aggregate) vs max Jaccard(unit, any_member)
                for i, unit in enumerate(story_units_list):
                    article = articles.get(str(unit.representative_article_id))
                    unit_entities = set()
                    if article and article.entities:
                        primary_entities = {k: v for k, v in article.entities.items() if k in {"PERSON", "ORG", "GPE"}}
                        for v in primary_entities.values():
                            unit_entities.update(v)

                    if not unit_entities or not aggregate_entities:
                        continue

                    jaccard_union = len(unit_entities & aggregate_entities) / len(unit_entities | aggregate_entities)

                    # Max Jaccard with any single member
                    max_jaccard_member = 0.0
                    for other_unit in story_units_list:
                        if other_unit.id == unit.id:
                            continue
                        other_article = articles.get(str(other_unit.representative_article_id))
                        other_entities = set()
                        if other_article and other_article.entities:
                            primary_entities = {k: v for k, v in other_article.entities.items() if k in {"PERSON", "ORG", "GPE"}}
                            for v in primary_entities.values():
                                other_entities.update(v)
                        if other_entities:
                            j = len(unit_entities & other_entities) / len(unit_entities | other_entities)
                            max_jaccard_member = max(max_jaccard_member, j)

                    total_checked += 1
                    # Would flip at threshold 0.4: union >= 0.4 but max_member < 0.4, or vice versa
                    union_passes = jaccard_union >= 0.4
                    member_passes = max_jaccard_member >= 0.4
                    if union_passes != member_passes:
                        flip_count += 1

        print(f"Stories with >=3 units checked: {total_checked}")
        print(f"Attach decisions that would flip at threshold 0.4: {flip_count}")

        # ============================================================
        # 6. Config facts
        # ============================================================
        print("\n[6] CONFIG FACTS")
        print("-" * 80)

        from src.shared.config import get_settings
        settings = get_settings()
        print(f"  top_n_entities: {settings.top_n_entities} (default 3, max 9 IDs per unit across PERSON/ORG/GPE)")
        print("  Jaccard similarity threshold: 0.4")
        print("  Story matching window: 48 hours")
        print(f"  Containment threshold (units): {settings.containment_threshold}")

    await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Grouping diagnostic report")
    parser.add_argument("--hours", type=int, default=48, help="Hours to look back (default: 48)")
    args = parser.parse_args()

    asyncio.run(run_report(args.hours))


if __name__ == "__main__":
    main()