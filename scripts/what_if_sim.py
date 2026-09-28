#!/usr/bin/env python3
"""What-if simulation for top_n_entities settings - optimized in-memory version."""

import asyncio
import os
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from dotenv import load_dotenv
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from src.schema.models import Story, ReportingUnit, RawArticle, StoryUnitLink

load_dotenv()


async def what_if_simulation():
    database_url = os.getenv('DATABASE_URL')
    engine = create_async_engine(database_url, echo=False, connect_args={'statement_cache_size': 0})
    async_session = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=168)

        # 1. Fetch all tier-1 units in one query
        stmt = select(ReportingUnit).where(ReportingUnit.created_at >= cutoff)
        result = await session.execute(stmt)
        units = result.scalars().all()

        unit_article_ids = [u.representative_article_id for u in units]
        stmt = select(RawArticle).where(RawArticle.id.in_(unit_article_ids))
        result = await session.execute(stmt)
        articles = {str(a.id): a for a in result.scalars().all()}

        from src.verification.units import get_owner_group
        from src.schema.models import SourceTier
        tier1_units = []
        for unit in units:
            article = articles.get(str(unit.representative_article_id))
            if article and article.source_tier == SourceTier.TIER1:
                owner = get_owner_group(article.source_domain)
                tier1_units.append((unit, article, owner))

        print(f'Tier-1 units: {len(tier1_units)}')

        # 2. Pull ALL StoryUnitLink rows ONCE into memory: unit_id -> set(story_ids)
        stmt = select(StoryUnitLink.unit_id, StoryUnitLink.story_id)
        result = await session.execute(stmt)
        unit_story_map = defaultdict(set)
        for unit_id, story_id in result.all():
            unit_story_map[str(unit_id)].add(str(story_id))

        # 3. Build entity sets for each top_n ONCE
        entity_sets_by_top_n = {}
        for top_n in [3, 5, 6]:
            entity_sets = []
            for unit, article, owner in tier1_units:
                entities = set()
                if article and article.entities:
                    primary_entities = {k: v for k, v in article.entities.items() if k in {'PERSON', 'ORG', 'GPE'}}
                    all_entities = []
                    for v in primary_entities.values():
                        all_entities.extend(v)
                    entities = set(all_entities[:top_n])
                entity_sets.append(entities)
            entity_sets_by_top_n[top_n] = entity_sets

        # 4. TF-IDF on titles (once)
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        titles = [u[1].title for u in tier1_units]
        vectorizer = TfidfVectorizer(stop_words='english', max_features=1000)
        tfidf_matrix = vectorizer.fit_transform(titles)
        cos_sim = cosine_similarity(tfidf_matrix)

        # 5. Build story -> units map from the in-memory unit_story_map
        story_unit_map = defaultdict(list)
        for idx, (unit, article, owner) in enumerate(tier1_units):
            unit_id = str(unit.id)
            for story_id in unit_story_map.get(unit_id, []):
                story_unit_map[story_id].append((idx, unit))

        # 6. For each top_n, analyze
        for top_n in [3, 5, 6]:
            print(f'\n=== top_n_entities = {top_n} ===')
            entity_sets = entity_sets_by_top_n[top_n]

            # Cross-owner pairs at cos >= 0.80 and cos >= 0.90
            cross_owner_cos_80 = 0
            cross_owner_cos_90 = 0
            for i in range(len(tier1_units)):
                for j in range(i + 1, len(tier1_units)):
                    if tier1_units[i][2] != tier1_units[j][2]:
                        if cos_sim[i, j] >= 0.80:
                            cross_owner_cos_80 += 1
                        if cos_sim[i, j] >= 0.90:
                            cross_owner_cos_90 += 1

            print(f'Cross-owner pairs at cos >= 0.80: {cross_owner_cos_80}')
            print(f'Cross-owner pairs at cos >= 0.90: {cross_owner_cos_90}')

            # Pairs already merged at Jaccard >= 0.4
            already_merged = 0
            newly_merged = []
            for i in range(len(tier1_units)):
                for j in range(i + 1, len(tier1_units)):
                    if tier1_units[i][2] != tier1_units[j][2]:
                        u1_id = str(tier1_units[i][0].id)
                        u2_id = str(tier1_units[j][0].id)
                        # In-memory check: already merged iff intersection non-empty
                        already = len(unit_story_map.get(u1_id, set()) & unit_story_map.get(u2_id, set())) > 0

                        e1 = entity_sets[i]
                        e2 = entity_sets[j]
                        if e1 or e2:
                            jaccard = len(e1 & e2) / len(e1 | e2) if (e1 | e2) else 0.0
                        else:
                            jaccard = 0.0

                        if jaccard >= 0.4:
                            if already:
                                already_merged += 1
                            else:
                                newly_merged.append({
                                    'pair': (i, j),
                                    'jaccard': jaccard,
                                    'cosine': cos_sim[i, j],
                                    'title1': tier1_units[i][1].title,
                                    'title2': tier1_units[j][1].title,
                                    'owner1': tier1_units[i][2],
                                    'owner2': tier1_units[j][2],
                                })

            print(f'Pairs already merged at Jaccard >= 0.4: {already_merged}')
            print(f'Newly merged pairs (Jaccard >= 0.4): {len(newly_merged)}')

            # False merge risk: newly merged pairs with cosine < 0.6
            false_merge_risk = [m for m in newly_merged if m['cosine'] < 0.6]
            print(f'False merge risk (cosine < 0.6): {len(false_merge_risk)}')

            # Show 10 examples
            for m in newly_merged[:10]:
                print(f'  Jaccard={m["jaccard"]:.3f}, cos={m["cosine"]:.3f}: {m["owner1"]} vs {m["owner2"]}')
                print(f'    T1: {m["title1"][:80]}')
                print(f'    T2: {m["title2"][:80]}')

            # Union-growth flip count
            flip_count = 0
            for story_id, unit_list in story_unit_map.items():
                if len(unit_list) >= 3:
                    agg_entities = set()
                    for idx, unit in unit_list:
                        agg_entities.update(entity_sets[idx])

                    for idx, unit in unit_list:
                        unit_e = entity_sets[idx]
                        if not unit_e or not agg_entities:
                            continue
                        j_union = len(unit_e & agg_entities) / len(unit_e | agg_entities)
                        max_member = 0
                        for idx2, unit2 in unit_list:
                            if idx2 == idx:
                                continue
                            e2 = entity_sets[idx2]
                            if e2:
                                j = len(unit_e & e2) / len(unit_e | e2)
                                max_member = max(max_member, j)

                        union_passes = j_union >= 0.4
                        member_passes = max_member >= 0.4
                        if union_passes != member_passes:
                            flip_count += 1

            print(f'Union-growth flips at top_n={top_n}: {flip_count}')

        # 7. Jaccard threshold sweep at top_n=3
        print('\n=== Jaccard threshold sweep at top_n=3 ===')
        entity_sets = entity_sets_by_top_n[3]
        for jaccard_threshold in [0.3, 0.25, 0.2]:
            already_merged = 0
            newly_merged = []
            for i in range(len(tier1_units)):
                for j in range(i + 1, len(tier1_units)):
                    if tier1_units[i][2] != tier1_units[j][2]:
                        u1_id = str(tier1_units[i][0].id)
                        u2_id = str(tier1_units[j][0].id)
                        already = len(unit_story_map.get(u1_id, set()) & unit_story_map.get(u2_id, set())) > 0

                        e1 = entity_sets[i]
                        e2 = entity_sets[j]
                        if e1 or e2:
                            jaccard = len(e1 & e2) / len(e1 | e2) if (e1 | e2) else 0.0
                        else:
                            jaccard = 0.0

                        if jaccard >= jaccard_threshold:
                            if already:
                                already_merged += 1
                            else:
                                newly_merged.append({
                                    'pair': (i, j),
                                    'jaccard': jaccard,
                                    'cosine': cos_sim[i, j],
                                    'title1': tier1_units[i][1].title,
                                    'title2': tier1_units[j][1].title,
                                    'owner1': tier1_units[i][2],
                                    'owner2': tier1_units[j][2],
                                })

            false_merge_risk = [m for m in newly_merged if m['cosine'] < 0.6]
            cross_owner_cos_80 = sum(1 for m in newly_merged if m['cosine'] >= 0.80)
            cross_owner_cos_90 = sum(1 for m in newly_merged if m['cosine'] >= 0.90)

            print(f'Jaccard >= {jaccard_threshold}: newly_merged={len(newly_merged)}, '
                  f'already_merged={already_merged}, '
                  f'cos>=0.80={cross_owner_cos_80}, cos>=0.90={cross_owner_cos_90}, '
                  f'false_merge_risk(cos<0.6)={len(false_merge_risk)}')
            for m in newly_merged[:10]:
                print(f'  Jaccard={m["jaccard"]:.3f}, cos={m["cosine"]:.3f}: {m["owner1"]} vs {m["owner2"]}')
                print(f'    T1: {m["title1"][:80]}')
                print(f'    T2: {m["title2"][:80]}')

    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(what_if_simulation())