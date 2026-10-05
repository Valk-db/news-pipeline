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

The new rows are then handed to the canonical identity pass
(``src/verification/event_identity.py``) in the same transaction, which points
each event at the event that represents it. That is what keeps the map honest as
it grows: without it every new story that names Tel Aviv adds another pin for
whatever is happening there, and the only way to stop was to re-run a script by
hand. The pass reads the whole table, so rows written by other producers take
part too -- the duplicates were never this script's rows alone.

Usage:
    uv run python scripts/backfill_globe_events.py --dry-run    # report only
    uv run python scripts/backfill_globe_events.py               # write rows
"""

import argparse
import asyncio
import uuid

from sqlalchemy import String, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB

from src.schema.models import CanonicalEntity, Event, EventLayer, Story
from src.shared.config import get_settings
from src.shared.database import _get_engine, _get_session_maker, get_session
from src.shared.analyzer_versions import GEOCODE_VERSION, compute_input_hash
from src.verification.event_identity import assign_canonical_events


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


def most_specific(entities: list[CanonicalEntity]) -> CanonicalEntity:
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
    story: Story, entity: CanonicalEntity, layer_id: uuid.UUID | None = None
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
        # An Event's only computed content is a point, and that point came out of the
        # geocoder -- so the geocoder is the analyzer that decides whether this row is
        # current, which is why the version here is the geocode one and not a globe-backfill
        # one. The hash covers the inputs the point was resolved from (story, place, the
        # coordinates themselves), not the tier counts, which are a projection of the story.
        analyzer_version=GEOCODE_VERSION,
        input_hash=compute_input_hash(
            GEOCODE_VERSION,
            str(story.id),
            entity.canonical_name,
            float(entity.latitude),
            float(entity.longitude),
        ),
    )


def group_by_story(rows) -> dict[uuid.UUID, tuple]:
    """Collapse (story, entity) rows into {story: (story, [entity, ...])}."""
    grouped: dict[uuid.UUID, tuple] = {}
    for story, entity in rows:
        if story.id not in grouped:
            grouped[story.id] = (story, [])
        grouped[story.id][1].append(entity)
    return grouped


async def backfill_events(dry_run: bool = True) -> int:
    """Write one Event per story that names a located entity. Returns the count.

    In the same transaction, point every event at the event that represents it, so
    the rows this writes cannot land as a second pin for an event that is already on
    the map. The pass is idempotent and reads the whole table, so re-running either
    half of this is safe.
    """
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
        # Flush so the new rows are visible to the pass, which reads them back with the
        # rest of the table instead of trusting the objects in hand.
        await session.flush()
        identity = await assign_canonical_events(session)
        await session.commit()

    print(f"Created {len(events)} events (layer: {layer_id or 'none'})")
    print(
        f"Canonical identity: {identity.canonical} events from {identity.inspected} rows "
        f"({identity.collapsed} collapsed)"
    )
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
