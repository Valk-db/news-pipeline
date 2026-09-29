#!/usr/bin/env python3
"""Build label sample for what-if simulation."""

import asyncio
import json
import os
import random
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from src.schema.models import ReportingUnit, RawArticle, StoryUnitLink

load_dotenv()


async def build_label_sample():
    database_url = os.getenv('DATABASE_URL')
    engine = create_async_engine(database_url, echo=False, connect_args={'statement_cache_size': 0})
    async_session = sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=168)

        # Fetch all tier-1 units
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

        # Pull ALL StoryUnitLink rows
        stmt = select(StoryUnitLink.unit_id, StoryUnitLink.story_id)
        result = await session.execute(stmt)
        unit_story_map = defaultdict(set)
        for unit_id, story_id in result.all():
            unit_story_map[str(unit_id)].add(str(story_id))

        # Build entity sets for top_n=5
        top_n = 5
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

        # TF-IDF on titles
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        titles = [u[1].title for u in tier1_units]
        vectorizer = TfidfVectorizer(stop_words='english', max_features=1000)
        tfidf_matrix = vectorizer.fit_transform(titles)
        cos_sim = cosine_similarity(tfidf_matrix)

        # Get sentence-transformer embeddings
        from src.enrichment.embedding_service import EmbeddingService
        embedding_service = EmbeddingService()

        texts = [u[1].title + ' ' + (u[1].body_text or '')[:500] for u in tier1_units]
        embeddings = await embedding_service.generate_embeddings(texts)

        def embedding_cosine(a, b):
            import numpy as np
            a_np = np.array(a)
            b_np = np.array(b)
            return float(np.dot(a_np, b_np) / (np.linalg.norm(a_np) * np.linalg.norm(b_np)))

        # Collect newly-merged pairs at Jaccard >= 0.4, stratified by embedding cosine
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

                    if jaccard >= 0.4 and not already:
                        emb_cos = embedding_cosine(embeddings[i], embeddings[j])
                        newly_merged.append({
                            'pair': (i, j),
                            'jaccard': jaccard,
                            'cosine': cos_sim[i, j],
                            'emb_cosine': emb_cos,
                            'title1': tier1_units[i][1].title,
                            'title2': tier1_units[j][1].title,
                            'owner1': tier1_units[i][2],
                            'owner2': tier1_units[j][2],
                            'url1': tier1_units[i][1].url,
                            'url2': tier1_units[j][1].url,
                            'body1': (tier1_units[i][1].body_text or '')[:200],
                            'body2': (tier1_units[j][1].body_text or '')[:200],
                            'pub1': tier1_units[i][1].published_at.isoformat() if tier1_units[i][1].published_at else '',
                            'pub2': tier1_units[j][1].published_at.isoformat() if tier1_units[j][1].published_at else '',
                        })

        # Stratify
        cos_lt_06 = [m for m in newly_merged if m['emb_cosine'] < 0.6]
        cos_06_08 = [m for m in newly_merged if 0.6 <= m['emb_cosine'] < 0.8]
        cos_ge_08 = [m for m in newly_merged if m['emb_cosine'] >= 0.8]

        random.seed(42)

        sample = []
        # 20 from cos<0.6
        sample.extend(random.sample(cos_lt_06, min(20, len(cos_lt_06))))
        # up to 15 from 0.6-0.8
        sample.extend(random.sample(cos_06_08, min(15, len(cos_06_08))))

        # Get already-merged controls
        already_merged_controls = []
        for i in range(len(tier1_units)):
            for j in range(i + 1, len(tier1_units)):
                if tier1_units[i][2] != tier1_units[j][2]:
                    u1_id = str(tier1_units[i][0].id)
                    u2_id = str(tier1_units[j][0].id)
                    already = len(unit_story_map.get(u1_id, set()) & unit_story_map.get(u2_id, set())) > 0
                    if already:
                        e1 = entity_sets[i]
                        e2 = entity_sets[j]
                        if e1 or e2:
                            jaccard = len(e1 & e2) / len(e1 | e2) if (e1 | e2) else 0.0
                        else:
                            jaccard = 0.0
                        if jaccard >= 0.4:
                            emb_cos = embedding_cosine(embeddings[i], embeddings[j])
                            if emb_cos >= 0.8:
                                already_merged_controls.append({
                                    'pair': (i, j),
                                    'jaccard': jaccard,
                                    'cosine': cos_sim[i, j],
                                    'emb_cosine': emb_cos,
                                    'title1': tier1_units[i][1].title,
                                    'title2': tier1_units[j][1].title,
                                    'owner1': tier1_units[i][2],
                                    'owner2': tier1_units[j][2],
                                    'url1': tier1_units[i][1].url,
                                    'url2': tier1_units[j][1].url,
                                    'body1': (tier1_units[i][1].body_text or '')[:200],
                                    'body2': (tier1_units[j][1].body_text or '')[:200],
                                    'pub1': tier1_units[i][1].published_at.isoformat() if tier1_units[i][1].published_at else '',
                                    'pub2': tier1_units[j][1].published_at.isoformat() if tier1_units[j][1].published_at else '',
                                })

        controls = random.sample(already_merged_controls, min(10, len(already_merged_controls)))
        sample.extend(controls)

        random.shuffle(sample)

        # Write sample file
        with open('docs/whatif_label_sample.md', 'w', encoding='utf-8') as f:
            f.write('# What-If Label Sample\n\n')
            f.write('45 pairs of (existing story, newly-attached unit) for human labeling.\n')
            f.write('Stratified by embedding cosine: 20 from cos<0.6, up to 15 from 0.6-0.8, 10 controls (already merged, cos>=0.8).\n')
            f.write('Shuffled. Stratum, cos, and J hidden from this file.\n\n')

            for idx, m in enumerate(sample[:45]):
                f.write(f'## Pair {idx+1}\n\n')
                f.write(f'**Story (existing):**\n')
                f.write(f'- Title: {m["title1"]}\n')
                f.write(f'- Source: {m["owner1"]}\n')
                f.write(f'- Published: {m["pub1"]}\n')
                f.write(f'- URL: {m["url1"]}\n')
                f.write(f'- Body (first 200 chars): {m["body1"]}\n\n')
                f.write(f'**Unit (newly attached):**\n')
                f.write(f'- Title: {m["title2"]}\n')
                f.write(f'- Source: {m["owner2"]}\n')
                f.write(f'- Published: {m["pub2"]}\n')
                f.write(f'- URL: {m["url2"]}\n')
                f.write(f'- Body (first 200 chars): {m["body2"]}\n\n')
                f.write(f'**same_event? (Y/N/UNSURE):** \n\n')
                f.write('---\n\n')

        # Write key file
        with open('docs/whatif_label_key.md', 'w', encoding='utf-8') as f:
            f.write('# What-If Label Key (DO NOT SHARE WITH LABELER)\n\n')
            f.write('Pair ID -> Stratum, Embedding Cosine, Jaccard\n\n')

            for idx, m in enumerate(sample[:45]):
                if m['emb_cosine'] < 0.6:
                    stratum = 'cos<0.6'
                elif m['emb_cosine'] < 0.8:
                    stratum = '0.6-0.8'
                else:
                    stratum = 'control (already merged, cos>=0.8)'
                f.write(f'{idx+1}: stratum={stratum}, emb_cos={m["emb_cosine"]:.3f}, jaccard={m["jaccard"]:.3f}\n')

        print(f'Total newly-merged: {len(newly_merged)}')
        print(f'cos<0.6: {len(cos_lt_06)}')
        print(f'0.6-0.8: {len(cos_06_08)}')
        print(f'cos>=0.8: {len(cos_ge_08)}')
        print(f'Controls available: {len(already_merged_controls)}')
        print(f'Sample size: {len(sample)}')

    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(build_label_sample())