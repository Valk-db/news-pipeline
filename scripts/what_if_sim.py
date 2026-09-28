#!/usr/bin/env python3
"""What-if simulation for top_n_entities settings."""

import asyncio
import os
from datetime import datetime, timezone, timedelta

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

        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        titles = [u[1].title for u in tier1_units]
        vectorizer = TfidfVectorizer(stop_words='english', max_features=1000)
        tfidf_matrix = vectorizer.fit_transform(titles)
        cos_sim = cosine_similarity(tfidf_matrix)

        # For each top_n, compute entity sets and check merges
        for top_n in [3, 5, 6]:
            print(f'\n=== top_n_entities = {top_n} ===')

            # Build entity sets (limited to top_n)
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

            # Find cross-outlet pairs that would merge at Jaccard >= 0.4
            newly_merged = []
            for i in range(len(tier1_units)):
                for j in range(i+1, len(tier1_units)):
                    if tier1_units[i][2] != tier1_units[j][2]:
                        # Check if already merged
                        u1_id = str(tier1_units[i][0].id)
                        u2_id = str(tier1_units[j][0].id)
                        stmt = select(Story.id).join(StoryUnitLink).where(StoryUnitLink.unit_id.in_([u1_id, u2_id])).group_by(Story.id).having(func.count(Story.id) == 2)
                        result = await session.execute(stmt)
                        stories = result.scalars().all()
                        already_merged = len(stories) == 1

                        # Compute Jaccard
                        e1 = entity_sets[i]
                        e2 = entity_sets[j]
                        if e1 or e2:
                            jaccard = len(e1 & e2) / len(e1 | e2) if (e1 | e2) else 0.0
                        else:
                            jaccard = 0.0

                        if jaccard >= 0.4 and not already_merged:
                            newly_merged.append({
                                'pair': (i, j),
                                'jaccard': jaccard,
                                'cosine': cos_sim[i, j],
                                'title1': tier1_units[i][1].title,
                                'title2': tier1_units[j][1].title,
                                'owner1': tier1_units[i][2],
                                'owner2': tier1_units[j][2],
                            })

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
        print('\n=== Union-growth flip counts ===')
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

            # Check stories with >=3 units for flip count
            story_unit_counts = {}
            for idx, (unit, article, owner) in enumerate(tier1_units):
                # Get story for this unit
                stmt = select(StoryUnitLink.story_id).where(StoryUnitLink.unit_id == unit.id)
                result = await session.execute(stmt)
                story_ids = [str(r[0]) for r in result.all()]
                for sid in story_ids:
                    if sid not in story_unit_counts:
                        story_unit_counts[sid] = []
                    story_unit_counts[sid].append((idx, unit))

            flip_count = 0
            for story_id, unit_list in story_unit_counts.items():
                if len(unit_list) >= 3:
                    # Aggregate entities
                    agg_entities = set()
                    for idx, unit in unit_list:
                        agg_entities.update(entity_sets[idx])

                    for idx, unit in unit_list:
                        unit_e = entity_sets[idx]
                        if not unit_e or not agg_entities:
                            continue
                        j_union = len(unit_e & agg_entities) / len(unit_e | agg_entities)
                        # Max with any member
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

            print(f'top_n={top_n}: union-growth flips = {flip_count}')

    await engine.dispose()

asyncio.run(what_if_simulation())