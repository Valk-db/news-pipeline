"""Topic group assignment for stories.

V1 uses keyword/entity matching against story.primary_entities. The stored
primary_entities are canonical entity UUID strings, so they are resolved to
their CanonicalEntity.canonical_name display names before matching --
matching keywords against raw UUIDs scores 0 on every keyword.
Future: upgrade to LLM-classified or claim-informed assignment.
"""

import json
import logging
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import Story, TopicGroup, StoryTopicGroup, CanonicalEntity
from src.shared.analyzer_versions import TOPIC_VERSION, compute_input_hash

logger = logging.getLogger(__name__)

# Keyword mapping for topic group assignment (name -> keywords)
# Maps topic group names to entity/keyword patterns
TOPIC_KEYWORDS = {
    "Geopolitics": ["country", "nation", "government", "state", "diplomat", "minister", "president", "prime minister", "foreign", "international", "treaty", "alliance", "summit"],
    "Geopolitics > Middle East": ["israel", "palestine", "gaza", "hamas", "hezbollah", "iran", "syria", "lebanon", "yemen", "saudi arabia", "uae", "qatar", "kuwait", "bahrain", "oman", "jordan", "iraq", "egypt"],
    "Geopolitics > Europe": ["ukraine", "russia", "eu", "european union", "nato", "germany", "france", "uk", "britain", "poland", "hungary", "turkey", "belarus", "moldova"],
    "Geopolitics > East Asia": ["china", "taiwan", "japan", "south korea", "north korea", "korea", "philippines", "vietnam", "indonesia", "malaysia", "singapore", "thailand", "myanmar"],
    "Economy": ["economy", "economic", "market", "stock", "inflation", "gdp", "recession", "growth", "trade", "tariff", "central bank", "interest rate", "fiscal", "monetary"],
    "Economy > Markets": ["stock market", "equity", "bond", "currency", "forex", "commodity", "oil price", "gold", "crypto", "bitcoin", "nasdaq", "dow jones", "s&p", "nikkei", "ftse"],
    "Economy > Trade Policy": ["trade deal", "tariff", "sanction", "export", "import", "wto", "trade war", "supply chain", "protectionism", "free trade"],
    "Health": ["health", "hospital", "disease", "virus", "pandemic", "vaccine", "who", "cdc", "covid", "cancer", "mental health", "healthcare", "medicine", "drug", "pharma"],
    "Environment & Disaster": ["climate", "climate change", "global warming", "carbon", "emission", "renewable", "solar", "wind", "disaster", "earthquake", "flood", "hurricane", "wildfire", "storm", "drought", "pollution"],
    "Technology": ["ai", "artificial intelligence", "tech", "technology", "software", "chip", "semiconductor", "quantum", "cyber", "data", "privacy", "encryption", "blockchain", "startup", "big tech"],
    "Domestic Politics (US)": ["congress", "senate", "house", "white house", "biden", "trump", "democrat", "republican", "election", "vote", "bill", "legislation", "supreme court", "federal", "state government"],
    # The two groups below cover the GDELT attention layers this taxonomy had
    # no home for: the diplomatic layer (COOPERATION 44.74% + VERBAL 22.60% +
    # DISAPPROVE 10.43% = 77.77% of 111,716 measured GDELT events, against
    # 8.54% of the 1,838 pipeline articles the GDELT batch attributed) and
    # the humanitarian layer (AID, 3,687 events, the 6th largest family). Both
    # sat under Geopolitics only by accident.
    #
    # Measured ceiling, stated here so the next person does not have to
    # rediscover it: _match_topic_groups() matches entity display names, not
    # article text, so a keyword only fires when an NER entity is named after
    # the institution or the process. Against the 3,916 distinct entity names
    # resolved from 1,669 dev stories, every pure process word (accord, envoy,
    # delegation, negotiation, peace talks, state visit, diplomat, security
    # council, resolution, arms control) scores ZERO. The institution names do
    # fire -- "united nations" 28 stories, "european union" 18, "nato" 14
    # word-bounded, "foreign ministry" 12, "state department" 10 -- so these
    # lists are institution-heavy on purpose and the process words are kept
    # only because they are how these stories read once a wire feed is
    # ingested. Measured effect on the real dev stories: +110
    # "Diplomacy & Multilateral", +13 "Humanitarian Aid & Development", and
    # 16 stories moved off the bare-Geopolitics fallback; no other group's
    # count moved by more than the 3 stories that stopped falling back.
    #
    # Bare short words are excluded on measured evidence, not taste: "aid"
    # matched "ali al-zaidi", "maidenhead" and "medicaid", "relief" matched
    # "fashion for relief", "ocha" matched "bochasanwasi akshar" and "miryam
    # shira ochayon", and "development" matched "webgis dashboard
    # development" and "jersey economic development authority" -- 8 of its 8
    # hits being false positives or marginal. Phrases are used where the bare
    # word is unsafe ("relief effort", "aid package", "development bank").
    "Diplomacy & Multilateral": [
        "united nations", "european union", "african union", "nato", "g7",
        "g20", "asean", "opec", "commonwealth", "foreign ministry",
        "state department", "embassy", "ambassador", "delegation", "treaty",
        "alliance", "bilateral", "envoy", "state visit", "peace talks",
        "security council", "arms control", "nonproliferation", "accord",
        "cooperation", "summit",
    ],
    "Humanitarian Aid & Development": [
        "humanitarian", "unhcr", "unicef", "wfp", "world food programme",
        "world food program", "red cross", "red crescent", "refugee",
        "refugees", "famine", "food security", "displaced", "aid worker",
        "aid package", "foreign aid", "disaster relief", "relief effort",
        "world bank", "imf", "international monetary fund",
        "multilateral development bank",
    ],
}

# Fallback generic group
FALLBACK_GROUP = "Geopolitics"


def _match_topic_groups(entities: list[str]) -> list[tuple[str, int]]:
    """Match story entity surface names to topic groups, returning list of
    (group_name, confidence).

    The entities MUST be display names (CanonicalEntity.canonical_name), not
    canonical entity UUID strings -- a UUID contains no English keyword, so
    passing raw primary_entities here scores 0 on every keyword and returns
    the fallback unconditionally. Use _resolve_entity_names() first.
    """
    if not entities:
        return [(FALLBACK_GROUP, 10)]  # Low confidence fallback

    entity_text = " ".join(entities).lower()
    matches = []

    for group_name, keywords in TOPIC_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            if keyword.lower() in entity_text:
                score += 1

        if score > 0:
            # Confidence: 10-100 based on keyword match count
            confidence = min(100, 10 + score * 15)
            matches.append((group_name, confidence))

    # Sort by confidence descending
    matches.sort(key=lambda x: x[1], reverse=True)

    if not matches:
        return [(FALLBACK_GROUP, 10)]

    return matches


async def _resolve_entity_names(
    session: AsyncSession, entities: list[str]
) -> list[str]:
    """Resolve story.primary_entities to surface names for keyword matching.

    primary_entities stores canonical entity UUID strings (see
    stories.create_story_from_units). Each UUID is looked up in
    canonical_entities and replaced by its canonical_name; entries that are
    not UUIDs (legacy surface-form data) pass through unchanged; UUIDs with
    no canonical row are dropped (they cannot match a keyword anyway).
    Non-list payloads (observed: a dict on some dev rows) are treated as
    empty rather than iterated.
    """
    if isinstance(entities, str):
        try:
            entities = json.loads(entities)
        except (ValueError, TypeError):
            entities = []
    if not isinstance(entities, (list, tuple)):
        return []

    canonical_ids: list[uuid.UUID] = []
    passthrough: list[str] = []
    for e in entities:
        try:
            canonical_ids.append(uuid.UUID(str(e)))
        except (ValueError, AttributeError, TypeError):
            passthrough.append(str(e))

    names = list(passthrough)
    if canonical_ids:
        stmt = select(CanonicalEntity.canonical_name).where(
            CanonicalEntity.id.in_(canonical_ids)
        )
        names.extend((await session.execute(stmt)).scalars().all())
    return names


async def assign_story_topic_groups(session: AsyncSession, story_id) -> list[dict]:
    """Assign topic groups to a story based on its primary_entities.

    Returns list of created assignments with confidence.
    """
    results = []

    stmt = select(Story).where(Story.id == story_id)
    story = (await session.execute(stmt)).scalar_one_or_none()
    if not story:
        return [{"story_id": str(story_id), "error": "Story not found"}]

    # primary_entities holds canonical UUID strings -- resolve to display
    # names before keyword matching, or every keyword scores 0 and every
    # story falls back to Geopolitics.
    entities = await _resolve_entity_names(session, story.primary_entities)
    matches = _match_topic_groups(entities)

    for group_name, confidence in matches:
        # Find the topic group
        stmt = select(TopicGroup).where(TopicGroup.name == group_name)
        result = await session.execute(stmt)
        topic_group = result.scalar_one_or_none()

        if not topic_group:
            logger.warning(f"Topic group '{group_name}' not found in database")
            continue

        # Check if already assigned
        stmt = select(StoryTopicGroup).where(
            StoryTopicGroup.story_id == story_id,
            StoryTopicGroup.topic_group_id == topic_group.id,
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            results.append({
                "story_id": str(story_id),
                "topic_group": group_name,
                "confidence": existing.confidence,
                "created": False,
            })
            continue

        # Create assignment
        assignment = StoryTopicGroup(
            story_id=story_id,
            topic_group_id=topic_group.id,
            confidence=confidence,
            # The input is the (story, topic group) pair -- the same key as
            # uq_story_topic_group -- because that is what the keyword match decided. The
            # confidence is the match strength, recorded on the row rather than hashed.
            analyzer_version=TOPIC_VERSION,
            input_hash=compute_input_hash(
                TOPIC_VERSION, str(story_id), str(topic_group.id)
            ),
        )
        session.add(assignment)
        await session.flush()
        results.append({
            "story_id": str(story_id),
            "topic_group": group_name,
            "confidence": confidence,
            "created": True,
        })

    # Only commit if we actually created new assignments
    created_any = any(r["created"] for r in results)
    if created_any:
        await session.commit()
    elif not results:
        # Fallback: assign to generic group if nothing matched
        stmt = select(TopicGroup).where(TopicGroup.name == FALLBACK_GROUP)
        result = await session.execute(stmt)
        fallback_group = result.scalar_one_or_none()
        if fallback_group:
            assignment = StoryTopicGroup(
                story_id=story_id,
                topic_group_id=fallback_group.id,
                confidence=10,
                analyzer_version=TOPIC_VERSION,
                input_hash=compute_input_hash(
                    TOPIC_VERSION, str(story_id), str(fallback_group.id)
                ),
            )
            session.add(assignment)
            await session.commit()
            results.append({
                "story_id": str(story_id),
                "topic_group": FALLBACK_GROUP,
                "confidence": 10,
                "created": True,
            })

    return results


async def assign_topic_groups_for_recent_stories(
    session_factory, hours_back: int = 168, max_stories: int = 100,
) -> list[dict]:
    """Batch assignment for recent gate-passed stories.

    Retargeted for Phase 2 (2026-10-03): the pool used to be QUEUED-only and
    selected nothing in practice. PENDING stories have passed the dynamic
    gate. Viewpoint child stories are excluded so spend is not multiplied
    across children that share one body of evidence.
    """
    from src.verification.phase2 import select_phase2_stories

    async with session_factory() as session:
        stories = await select_phase2_stories(
            session, hours_back=hours_back, max_stories=max_stories
        )

    story_ids = [s.id for s in stories]

    if not story_ids:
        logger.info("No gate-passed stories to assign topic groups for")
        return []

    logger.info(
        f"Assigning topic groups for {len(story_ids)} gate-passed stories"
    )

    results = []
    for story_id in story_ids:
        async with session_factory() as session:
            result = await assign_story_topic_groups(session, story_id)
            results.extend(result)

    return results