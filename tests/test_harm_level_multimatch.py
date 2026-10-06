"""Reproduction tests for P0-2: compute_harm_level crashes on ordinary stories.

Both queries in compute_harm_level used scalar_one_or_none(), which raises
sqlalchemy.exc.MultipleResultsFound when more than one row matches. A story
routinely has several ALLEGATION claims (claims.py writes one row per extracted
claim) and several PERSON entities (e.g. a story about Trump and Biden), so the
function blew up inside apply_dynamic_gate's unguarded loop: the
await session.commit() at the end of the gate never ran and every story after
the failing one went ungated.

These tests run against a real (in-memory SQLite) database so the multi-row
queries genuinely return 2+ rows. They fail with MultipleResultsFound on the
unfixed code and pass once the queries take the first row (LIMIT 1) instead.
"""

import uuid
from datetime import datetime, UTC

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.schema.models import Base, Story, Claim, ClaimType, CanonicalEntity
from src.verification.tiers import compute_harm_level


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _person(name):
    return CanonicalEntity(
        id=uuid.uuid4(),
        canonical_name=name,
        entity_type="PERSON",
    )


async def _story_with(db_session, n_allegations, n_persons):
    persons = [_person(f"Person {i}") for i in range(n_persons)]
    story = Story(
        id=uuid.uuid4(),
        day=datetime.now(UTC),
        primary_entities=[str(p.id) for p in persons],
        status=Story.Status.PENDING,
    )
    db_session.add(story)
    for p in persons:
        db_session.add(p)
    for i in range(n_allegations):
        db_session.add(
            Claim(
                id=uuid.uuid4(),
                story_id=story.id,
                claim_type=ClaimType.ALLEGATION,
                text=f"Allegation {i}",
            )
        )
    await db_session.commit()
    return story


@pytest.mark.asyncio
async def test_harm_level_multiple_allegations(db_session):
    """2 ALLEGATION claims + 1 PERSON: the old claim query raised here."""
    story = await _story_with(db_session, n_allegations=2, n_persons=1)
    assert await compute_harm_level(db_session, story) == "high"


@pytest.mark.asyncio
async def test_harm_level_multiple_persons(db_session):
    """1 ALLEGATION claim + 2 PERSON entities (Trump-and-Biden case): the old
    entity query raised here."""
    story = await _story_with(db_session, n_allegations=1, n_persons=2)
    assert await compute_harm_level(db_session, story) == "high"


@pytest.mark.asyncio
async def test_harm_level_multiple_allegations_and_persons(db_session):
    """2 ALLEGATION claims + 2 PERSON entities: both old queries raised."""
    story = await _story_with(db_session, n_allegations=2, n_persons=2)
    assert await compute_harm_level(db_session, story) == "high"


@pytest.mark.asyncio
async def test_harm_level_multiple_allegations_no_person_still_low(db_session):
    """2 ALLEGATION claims but no PERSON entity: 'low', no exception."""
    story = await _story_with(db_session, n_allegations=2, n_persons=0)
    story.primary_entities = []
    assert await compute_harm_level(db_session, story) == "low"
