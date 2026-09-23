#!/usr/bin/env python3
"""
One-time script to recompute story counters from current StoryUnitLinks.

Usage:
    python -m scripts.recompute_counters
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from src.schema.models import Story
from src.shared.database import prepare_database_url
from src.shared.config import get_settings
from src.verification.tiers import recompute_story_counters


async def recompute_all():
    settings = get_settings()
    db_url = settings.database_url
    url, connect_args = prepare_database_url(db_url)
    engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Get all story IDs
        result = await session.execute(select(Story.id))
        story_ids = [row[0] for row in result.all()]
        print(f'Found {len(story_ids)} stories to recompute')

        # Recompute in batches
        batch_size = 50
        for i in range(0, len(story_ids), batch_size):
            batch = story_ids[i:i+batch_size]
            await recompute_story_counters(session, batch)
            await session.commit()
            print(f'Recomputed batch {i//batch_size + 1}/{(len(story_ids)+batch_size-1)//batch_size} ({len(batch)} stories)')

    await engine.dispose()
    print('Done!')


if __name__ == "__main__":
    asyncio.run(recompute_all())