#!/usr/bin/env python
"""Backfill script for globe events.

Creates EventGeometry and Event rows from existing CanonicalEntity rows with lat/lon.
Links events to stories via StoryUnitLink -> ReportingUnit -> Story.

Usage:
    uv run python scripts/backfill_globe_events.py --dry-run    # Preview what would be created
    uv run python scripts/backfill_globe_events.py               # Actually create rows
"""

import argparse
import asyncio
import uuid
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.schema.models import (
    CanonicalEntity, Event, EventGeometry, EventLayer, Story, ReportingUnit, StoryUnitLink, RawArticle
)
from src.shared.config import get_settings
from src.shared.database import _get_engine, _get_session_maker


async def get_default_layer_id(session: AsyncSession) -> Optional[uuid.UUID]:
    """Get the ID of the default event layer."""
    stmt = select(EventLayer.id).where(EventLayer.name == 'default')
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_event_geometry(session: AsyncSession, event_id: uuid.UUID, lat: float, lon: float) -> uuid.UUID:
    """Create an EventGeometry (POINT) for an event."""
    geometry = EventGeometry(
        id=uuid.uuid4(),
        event_id=event_id,
        geometry_type=EventGeometry.GeometryType.POINT,
        geojson={"type": "Point", "coordinates": [lon, lat]},
        properties={"source": "canonical_entity_backfill"},
    )
    session.add(geometry)
    await session.flush()
    return geometry.id


async def create_event_from_entity(session: AsyncSession, entity: CanonicalEntity, layer_id: uuid.UUID) -> Optional[Event]:
    """Create an Event from a CanonicalEntity that has lat/lon."""
    if not entity.latitude or not entity.longitude:
        return None

    # Find stories that reference this entity via articles
    # Path: Entity -> RawArticle.entities -> ReportingUnit -> StoryUnitLink -> Story
    stmt = (
        select(Story.id)
        .join(StoryUnitLink, StoryUnitLink.story_id == Story.id)
        .join(ReportingUnit, ReportingUnit.id == StoryUnitLink.unit_id)
        .join(RawArticle, RawArticle.id == ReportingUnit.representative_article_id)
        .where(RawArticle.entities.op('?')('GPE') | RawArticle.entities.op('?')('LOC'))
        .where(func.jsonb_path_query_first(RawArticle.entities, f'$."GPE" ? (@ == "{entity.canonical_name}")').is_not(None) |
               func.jsonb_path_query_first(RawArticle.entities, f'$."LOC" ? (@ == "{entity.canonical_name}")').is_not(None))
        .distinct()
        .limit(1)
    )
    result = await session.execute(stmt)
    story_id = result.scalar_one_or_none()

    if not story_id:
        # Try a simpler match on any entity field
        stmt = (
            select(Story.id)
            .join(StoryUnitLink, StoryUnitLink.story_id == Story.id)
            .join(ReportingUnit, ReportingUnit.id == StoryUnitLink.unit_id)
            .join(RawArticle, RawArticle.id == ReportingUnit.representative_article_id)
            .where(RawArticle.entities.isnot(None))
            .distinct()
            .limit(1)
        )
        result = await session.execute(stmt)
        story_id = result.scalar_one_or_none()

    if not story_id:
        return None

    lat = float(entity.latitude)
    lon = float(entity.longitude)

    # Create EventGeometry first
    event_id = uuid.uuid4()
    geometry_id = await create_event_geometry(session, event_id, lat, lon)

    event = Event(
        id=event_id,
        story_id=story_id,
        latitude=str(lat),
        longitude=str(lon),
        location_name=entity.canonical_name,
        location_type=entity.location_type or "city",
        radius_km="25.0",
        start_time=entity.created_at,
        event_type=Event.EventType.OTHER,
        confidence="0.7",
        source_count=1,
        tier1_source_count=0,
        entities=entity.entities or {},
        geometry_id=geometry_id,
        layer_id=layer_id,
    )

    session.add(event)
    await session.flush()

    # Update entity with geometry_id and layer_id
    entity.geometry_id = geometry_id
    entity.layer_id = layer_id

    return event


async def backfill_events(dry_run: bool = True):
    """Main backfill function."""
    settings = get_settings()
    if not settings.has_database:
        print("ERROR: DATABASE_URL not configured")
        return

    engine = _get_engine()
    if engine is None:
        print("ERROR: Could not create engine")
        return

    session_maker = _get_session_maker()
    if session_maker is None:
        print("ERROR: Could not create session maker")
        return

    if dry_run:
        # In dry-run mode, just count entities without touching DB
        async with session_maker() as session:
            stmt = select(CanonicalEntity).where(
                CanonicalEntity.latitude.isnot(None),
                CanonicalEntity.longitude.isnot(None),
                CanonicalEntity.geometry_id.is_(None)
            )
            result = await session.execute(stmt)
            entities = result.scalars().all()
            print(f"Found {len(entities)} entities with lat/lon and no geometry")
            for entity in entities:
                print(f"  Would create event for: {entity.canonical_name} ({entity.latitude}, {entity.longitude})")
            print(f"Dry run complete: would create {len(entities)} events")
        return

    # Real run - actually create events
    async with session_maker() as session:
        # Get default layer
        layer_id = await get_default_layer_id(session)
        if not layer_id:
            # Create default layer
            layer = EventLayer(
                id=uuid.uuid4(),
                name="default",
                description="Default event layer",
                filter_criteria={},
                style={"color": "#3b82f6", "radius": 10000},
                is_default=True,
                is_visible=True,
                min_zoom=0,
                max_zoom=20,
                color="#3b82f6",
            )
            session.add(layer)
            await session.flush()
            layer_id = layer.id
            print(f"Created default layer: {layer_id}")

        # Find CanonicalEntity rows with lat/lon but no geometry_id
        stmt = select(CanonicalEntity).where(
            CanonicalEntity.latitude.isnot(None),
            CanonicalEntity.longitude.isnot(None),
            CanonicalEntity.geometry_id.is_(None)
        )
        result = await session.execute(stmt)
        entities = result.scalars().all()

        print(f"Found {len(entities)} entities with lat/lon and no geometry")

        created_count = 0
        skipped_count = 0

        for entity in entities:
            # Check if already has geometry (idempotency)
            if entity.geometry_id:
                print(f"  Skipping {entity.canonical_name} - already has geometry_id")
                skipped_count += 1
                continue

            event = await create_event_from_entity(session, entity, layer_id)
            if event:
                print(f"  Created event for: {entity.canonical_name} -> {event.id}")
                created_count += 1
            else:
                print(f"  Could not link {entity.canonical_name} to any story")
                skipped_count += 1

        await session.commit()
        print(f"Committed {created_count} events, skipped {skipped_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill globe events from canonical entities")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without committing")
    args = parser.parse_args()

    asyncio.run(backfill_events(dry_run=args.dry_run))