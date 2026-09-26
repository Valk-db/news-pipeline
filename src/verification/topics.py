"""Topic group assignment for stories.

V1 uses keyword/entity matching against story.primary_entities.
Future: upgrade to LLM-classified or claim-informed assignment.
"""

import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import Story, TopicGroup, StoryTopicGroup

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
}

# Fallback generic group
FALLBACK_GROUP = "Geopolitics"


def _match_topic_groups(entities: list[str]) -> list[tuple[str, int]]:
    """Match story entities to topic groups, returning list of (group_name, confidence)."""
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


async def assign_story_topic_groups(session: AsyncSession, story_id) -> list[dict]:
    """Assign topic groups to a story based on its primary_entities.

    Returns list of created assignments with confidence.
    """
    results = []

    stmt = select(Story).where(Story.id == story_id)
    story = (await session.execute(stmt)).scalar_one_or_none()
    if not story:
        return [{"story_id": str(story_id), "error": "Story not found"}]

    entities = list(story.primary_entities or [])
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
    """Batch assignment for recent QUEUED stories."""
    from datetime import datetime, timezone, timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    async with session_factory() as session:
        stmt = (
            select(Story)
            .where(Story.status == Story.Status.QUEUED)
            .where(Story.created_at >= cutoff)
            .order_by(Story.created_at.desc())
            .limit(max_stories)
        )
        result = await session.execute(stmt)
        stories = result.scalars().all()

    story_ids = [s.id for s in stories]

    if not story_ids:
        logger.info("No QUEUED stories to assign topic groups for")
        return []

    logger.info(f"Assigning topic groups for {len(story_ids)} QUEUED stories")

    results = []
    for story_id in story_ids:
        async with session_factory() as session:
            result = await assign_story_topic_groups(session, story_id)
            results.extend(result)

    return results