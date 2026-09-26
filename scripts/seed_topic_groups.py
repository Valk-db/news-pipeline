#!/usr/bin/env python
"""Seed topic groups hierarchy (idempotent).

Usage:
    uv run python scripts/seed_topic_groups.py
"""

import asyncio
import uuid
from src.shared.database import init_db, get_session_maker
from src.schema.models import TopicGroup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


TOPIC_GROUPS = [
    # (name, parent_name_or_none)
    ("Geopolitics", None),
    ("Geopolitics > Middle East", "Geopolitics"),
    ("Geopolitics > Europe", "Geopolitics"),
    ("Geopolitics > East Asia", "Geopolitics"),
    ("Economy", None),
    ("Economy > Markets", "Economy"),
    ("Economy > Trade Policy", "Economy"),
    ("Health", None),
    ("Environment & Disaster", None),
    ("Technology", None),
    ("Domestic Politics (US)", None),
]


async def seed_topic_groups(session: AsyncSession) -> int:
    """Seed topic groups, returning count of newly created groups."""
    created = 0
    name_to_id = {}

    for name, parent_name in TOPIC_GROUPS:
        # Check if already exists
        parent_id = name_to_id.get(parent_name)
        stmt = select(TopicGroup).where(TopicGroup.name == name)
        if parent_id:
            stmt = stmt.where(TopicGroup.parent_group_id == parent_id)
        else:
            stmt = stmt.where(TopicGroup.parent_group_id.is_(None))

        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            name_to_id[name] = existing.id
            continue

        # Create new group
        group = TopicGroup(
            id=uuid.uuid4(),
            name=name,
            parent_group_id=parent_id,
            description=None,
        )
        session.add(group)
        await session.flush()
        name_to_id[name] = group.id
        created += 1
        print(f"Created: {name}")

    await session.commit()
    return created


async def main() -> None:
    await init_db()
    session_factory = get_session_maker()
    async with session_factory() as session:
        created = await seed_topic_groups(session)
    print(f"Seeded {created} new topic groups (total hierarchy has {len(TOPIC_GROUPS)} groups)")


if __name__ == "__main__":
    asyncio.run(main())