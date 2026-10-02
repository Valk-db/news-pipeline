#!/usr/bin/env python
"""Put located canonical entities on the map, one point per story.

A story's ``primary_entities`` are the canonical entity ids that defined it, so
the stories that name a located place are exactly the ones whose
``primary_entities`` contain that entity. For every such story with no Event
yet, this writes one Event at the most specific located entity it names, and
copies the story's own tier counts onto the event so the events' corroboration
filter means the same thing for events as it does for stories.

What the point means: the geocoded location of a place the story NAMES. It is
not a claim that anything happened there, and nothing here infers one.
``event_type`` stays OTHER because the pipeline does not classify the event, and
``confidence`` is the story's tier-1 corroboration ratio, which is the only
evidence this path actually has.

Usage:
    uv run python scripts/backfill_globe_events.py --dry-run    # report only
    uv run python scripts/backfill_globe_events.py               # write rows
"""

import argparse
import asyncio
import uuid
from typing import Dict, List, Optional

from sqlalchemy import String, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB

from src.schema.models import CanonicalEntity, Event, EventLayer, Story
from src.shared.config import get_settings
from src.shared.database import _get_engine, _get_session_maker, get_session


# How fine-grained a geocoded place is, finest first. Nominatim's own label is
# the only granularity signal the entity carries, so this is the ranking: a story
# that names both "United States" and "Tel Aviv" dots Tel Aviv rather than the
# middle of Kansas. Unrecognized labels rank last so they cannot win a tie.
PLACE_SPECIFICITY = {
    "city": 0,
    "town": 1,
    "village": 2,
    "hamlet": 3,
    "suburb": 4,
    "municipality": 5,
    "county": 6,
    "province": 7,
    "state": 8,
    "region": 9,
    "administrative": 10,
    "country": 11,
    "island": 12,
    "peninsula": 13,
    "continent": 14,
}

UNKNOWN_SPECIFICITY = 99

# Located GPEs a story names, joined through the canonical ids the story was
# grouped by. Stories that already carry an Event are excluded, which is both the
# idempotency guard for re-runs and the reason this can never add a second point
# to a story another producer has already placed.
LOCATED_ENTITIES_FOR_STORIES = (
    select(Story, CanonicalEntity)
    .join(
        CanonicalEntity,
        (CanonicalEntity.entity_type == "GPE")
        & CanonicalEntity.latitude.isnot(None)
        & CanonicalEntity.longitude.isnot(None)
        & cast(Story.primary_entities, JSONB).op("@>")(
            func.jsonb_build_array(cast(CanonicalEntity.id, String))
        ),
    )
    .where(~select(Event.id).where(Event.story_id == Story.id).exists())
)


def most_specific(entities: List[CanonicalEntity]) -> CanonicalEntity:
    """The place a story should be dotted at.

    Finest granularity first, so a story naming both "United States" and "Tel
    Aviv" dots Tel Aviv rather than the middle of Kansas. Within one
    granularity the geocoder's own prominence decides, which is what keeps
    "Man City" (a village in Cote d'Ivoire) from beating "Manchester City" on
    the alphabet. The name is the last resort, so two runs over the same data
    always pick the same place.
    """
    return min(
        entities,
        key=lambda entity: (
            PLACE_SPECIFICITY.get(
                (entity.location_type or "").lower(), UNKNOWN_SPECIFICITY
            ),
            -(entity.geo_importance or 0.0),
            entity.canonical_name,
        ),
    )


def story_event(
    story: Story, entity: CanonicalEntity, layer_id: Optional[uuid.UUID] = None
) -> Event:
    """The single Event a story gets, from its most specific located entity."""
    units = sum(
        count or 0
        for count in (
            story.tier1_unit_count,
            story.tier2_unit_count,
            story.tier3_unit_count,
            story.tier4_unit_count,
        )
    )
    tier1 = story.tier1_unit_count or 0

    return Event(
        id=uuid.uuid4(),
        story_id=story.id,
        latitude=float(entity.latitude),
        longitude=float(entity.longitude),
        location_name=entity.canonical_name,
        location_type=entity.location_type,
        start_time=story.day,
        event_type=Event.EventType.OTHER,
        # How well corroborated the story is, not how sure the geocoder was: an
        # entity-anchored point is only as good as the sourcing behind it.
        confidence=tier1 / units if units else 0.0,
        source_count=units,
        tier1_source_count=tier1,
        entities={entity.entity_type: [entity.canonical_name]},
        layer_id=layer_id,
    )


def group_by_story(rows) -> Dict[uuid.UUID, tuple]:
    """Collapse (story, entity) rows into {story: (story, [entity, ...])}."""
    grouped: Dict[uuid.UUID, tuple] = {}
    for story, entity in rows:
        if story.id not in grouped:
            grouped[story.id] = (story, [])
        grouped[story.id][1].append(entity)
    return grouped


async def backfill_events(dry_run: bool = True) -> int:
    """Write one Event per story that names a located entity. Returns the count."""
    settings = get_settings()
    if not settings.has_database:
        raise SystemExit("ERROR: DATABASE_URL not configured")
    if _get_engine() is None or _get_session_maker() is None:
        raise SystemExit("ERROR: could not create the database engine")

    async with get_session() as session:
        by_story = group_by_story((await session.execute(LOCATED_ENTITIES_FOR_STORIES)).all())
        if not by_story:
            print("No stories name a located entity yet")
            return 0

        # Events render without a layer, so a missing default layer is reported
        # rather than invented.
        layer_id = (
            await session.execute(
                select(EventLayer.id).where(EventLayer.is_default).limit(1)
            )
        ).scalar_one_or_none()

        if dry_run:
            for story, entities in by_story.values():
                chosen = most_specific(entities)
                print(
                    f"  would place story {story.id} ({story.day.date()}) at "
                    f"{chosen.canonical_name} ({chosen.latitude}, {chosen.longitude}) "
                    f"from {len(entities)} located entities"
                )
            print(f"Dry run: {len(by_story)} events would be created")
            return len(by_story)

        events = [
            story_event(story, most_specific(entities), layer_id)
            for story, entities in by_story.values()
        ]
        session.add_all(events)
        await session.commit()

    print(f"Created {len(events)} events (layer: {layer_id or 'none'})")
    return len(events)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create map events from located canonical entities."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be created and stop."
    )
    args = parser.parse_args()

    asyncio.run(backfill_events(dry_run=args.dry_run))
