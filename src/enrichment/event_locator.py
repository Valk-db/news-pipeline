"""Event location inference from article content."""

import re
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone

from src.enrichment.geocoder import get_geocoder, geocode_entity
from src.shared.llm import get_llm_client

logger = logging.getLogger(__name__)


# Event type classification patterns
EVENT_TYPE_KEYWORDS = {
    "conflict": ["war", "battle", "attack", "strike", "offensive", "invasion", "clash", "combat", "military", "troops", "missile", "artillery", "airstrike"],
    "protest": ["protest", "demonstration", "rally", "march", "strike", "walkout", "sit-in", "occupation", "civil disobedience", "activist"],
    "election": ["election", "vote", "poll", "referendum", "ballot", "candidate", "campaign", "voting", "primary", "caucus"],
    "disaster": ["earthquake", "flood", "hurricane", "typhoon", "cyclone", "tornado", "wildfire", "landslide", "tsunami", "volcano", "storm"],
    "accident": ["crash", "collision", "derailment", "explosion", "fire", "accident", "wreck", "spill", "leak"],
    "political": ["summit", "treaty", "agreement", "diplomatic", "meeting", "negotiation", "sanctions", "embargo", "alliance"],
    "economic": ["market", "stock", "crash", "recession", "inflation", "gdp", "unemployment", "interest rate", "central bank", "trade"],
    "health": ["pandemic", "outbreak", "virus", "disease", "epidemic", "vaccine", "hospital", "health emergency", "who"],
    "environmental": ["climate", "carbon", "emissions", "pollution", "biodiversity", "extinction", "conservation", "greenhouse"],
    "crime": ["terrorist", "terrorism", "attack", "bombing", "shooting", "assassination", "kidnapping", "hijacking"],
    "sports": ["olympics", "world cup", "championship", "tournament", "final", "medal", "athlete", "team"],
    "cultural": ["festival", "ceremony", "celebration", "holiday", "parade", "arts", "music", "film"],
    "scientific": ["discovery", "breakthrough", "research", "study", "experiment", "nasa", "space", "launch", "satellite"],
}


def classify_event_type(text: str) -> Tuple[str, float]:
    """
    Classify event type from text using keyword matching.

    Returns:
        (event_type, confidence)
    """
    text_lower = text.lower()
    scores = {}

    for event_type, keywords in EVENT_TYPE_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            if keyword in text_lower:
                score += 1
        if score > 0:
            scores[event_type] = score

    if not scores:
        return "other", 0.3

    # Get top type
    best_type = max(scores, key=scores.get)
    max_score = scores[best_type]

    # Normalize confidence (max possible is len(keywords))
    confidence = min(max_score / 5.0, 1.0)

    return best_type, confidence


async def infer_event_type_llm(text: str, title: str) -> Tuple[str, float]:
    """
    Use LLM to classify event type for higher accuracy.

    Returns:
        (event_type, confidence)
    """
    llm = await get_llm_client()

    prompt = f"""Classify this news article into an event type.

Title: {title}
Text: {text[:2000]}

Event types:
- conflict: war, battle, attack, military action
- protest: demonstration, rally, strike, civil unrest
- election: voting, referendum, campaign
- disaster: earthquake, flood, hurricane, wildfire
- accident: crash, explosion, industrial accident
- political: summit, treaty, diplomatic meeting
- economic: market crash, policy, recession
- health: pandemic, outbreak, health emergency
- environmental: climate, pollution, conservation
- crime: terrorism, assassination, major crime
- sports: olympics, championship, major tournament
- cultural: festival, ceremony, major event
- scientific: breakthrough, space launch, discovery

Return JSON: {{"event_type": "type", "confidence": 0-100, "reasoning": "brief explanation"}}"""

    try:
        response = await llm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )

        content = response.choices[0].message.content.strip()
        import json
        result = json.loads(content)

        return result.get("event_type", "other"), result.get("confidence", 50) / 100.0

    except Exception as e:
        logger.warning(f"LLM event classification failed: {e}")
        return "other", 0.3


def extract_location_candidates(text: str, entities: Dict[str, List[str]]) -> List[str]:
    """
    Extract potential location mentions from text and entities.

    Priority order:
    1. GPE (Geopolitical Entities) from NER
    2. LOC (Locations) from NER
    3. Capitalized words near location prepositions
    """
    candidates = []

    # 1. NER entities
    if entities:
        for gpe in entities.get("GPE", []):
            candidates.append(gpe)
        for loc in entities.get("LOC", []):
            candidates.append(loc)
        # Also check FAC (facilities) and ORG that might be locations
        for fac in entities.get("FAC", []):
            candidates.append(fac)

    # 2. Pattern-based extraction for locations mentioned in context
    # e.g., "in Paris", "at New York", "near London", "from Beijing"
    location_patterns = [
        r'\b(?:in|at|near|from|to|towards|outside|inside|over|under)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)',
        r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:saw|witnessed|experienced|reported|hit|struck)',
    ]

    for pattern in location_patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            candidates.append(match.strip())

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        c_lower = c.lower()
        if c_lower not in seen and len(c) > 2:
            seen.add(c_lower)
            unique.append(c)

    return unique[:10]  # Limit candidates


async def locate_event_from_article(
    article_id: str,
    title: str,
    body_text: str,
    entities: Dict[str, List[str]],
    published_at: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """
    Infer event location and details from an article.

    Returns:
        Dict with event details or None if no location found
    """
    # Extract location candidates
    candidates = extract_location_candidates(body_text, entities)

    if not candidates:
        return None

    # Try geocoding candidates
    geocoder = get_geocoder()
    best_location = None

    for candidate in candidates:
        geo_result = await geocoder.geocode(candidate)
        if geo_result:
            best_location = geo_result
            break

    if not best_location:
        # Try LLM to extract location context
        llm_location = await _extract_location_llm(title, body_text[:1500])
        if llm_location:
            geo_result = await geocode_entity(llm_location, "LOC")
            if geo_result:
                best_location = await get_geocoder().geocode(llm_location)

    if not best_location:
        return None

    # Classify event type
    event_type, type_confidence = classify_event_type(body_text)

    # Optionally use LLM for better classification
    if type_confidence < 0.7:
        llm_type, llm_conf = await infer_event_type_llm(body_text[:2000], title)
        if llm_conf > type_confidence:
            event_type = llm_type
            type_confidence = llm_conf

    # Determine event time
    event_time = published_at or datetime.now(timezone.utc)

    # Determine radius based on location type
    radius_km = _estimate_radius(best_location.location_type)

    return {
        "latitude": best_location.latitude,
        "longitude": best_location.longitude,
        "location_name": best_location.name,
        "location_type": best_location.location_type,
        "radius_km": radius_km,
        "event_type": event_type,
        "confidence": type_confidence * 0.8 + 0.2,  # Combine with location confidence
        "event_time": event_time,
        "source": "article",
    }


def _estimate_radius(location_type: str) -> float:
    """Estimate event radius in km based on location type."""
    radius_map = {
        "point": 1.0,
        "city": 25.0,
        "town": 10.0,
        "village": 5.0,
        "country": 500.0,
        "state": 100.0,
        "region": 200.0,
        "county": 50.0,
        "area": 50.0,
    }
    return radius_map.get(location_type.lower(), 25.0)


async def _extract_location_llm(title: str, text: str) -> Optional[str]:
    """Use LLM to extract primary location from article."""
    llm = await get_llm_client()

    prompt = f"""Extract the PRIMARY geographic location where the main event occurred.

Title: {title}
Text: {text}

Return only the location name (city, region, or country) where the main event happened.
If multiple locations, return the most specific one where the main action occurred.
Return ONLY the location name, nothing else. If unclear, return "UNKNOWN"."""

    try:
        response = await llm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=50,
        )

        location = response.choices[0].message.content.strip()
        if location and location != "UNKNOWN":
            return location

    except Exception as e:
        logger.warning(f"LLM location extraction failed: {e}")

    return None


async def create_event_from_story(
    session,
    story_id: str,
    max_articles: int = 5,
) -> Optional[Dict[str, Any]]:
    """
    Create an Event from a Story by analyzing its articles.

    Args:
        session: Database session
        story_id: Story UUID
        max_articles: Max articles to analyze

    Returns:
        Created Event data or None
    """
    from sqlalchemy import select
    from src.schema.models import Story, RawArticle, ReportingUnit, StoryUnitLink, Event

    # Get story with articles
    stmt = (
        select(Story)
        .where(Story.id == story_id)
    )
    result = await session.execute(stmt)
    story = result.scalar_one_or_none()
    if not story:
        return None

    # Get linked articles
    stmt = (
        select(RawArticle)
        .join(ReportingUnit, RawArticle.id == ReportingUnit.representative_article_id)
        .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .where(StoryUnitLink.story_id == story_id)
        .limit(10)
    )
    result = await session.execute(stmt)
    articles = result.scalars().all()

    if not articles:
        return None

    # Analyze each article for location
    location_votes = {}
    event_type_votes = {}
    all_entities = {}

    for article in articles[:max_articles]:
        if not article.body_text:
            continue

        # Extract location
        entities = article.entities or {}
        location_result = await locate_event_from_article(
            article_id=str(article.id),
            title=article.title,
            body_text=article.body_text,
            entities=entities,
            published_at=article.published_at,
        )

        if location_result:
            key = (round(location_result["latitude"], 4), round(location_result["longitude"], 4))
            if key not in location_votes:
                location_votes[key] = {"data": location_result, "count": 0}
            location_votes[key]["count"] += 1

            # Aggregate event types
            etype = location_result.get("event_type", "other")
            event_type_votes[etype] = event_type_votes.get(etype, 0) + 1

            # Aggregate entities
            if article.entities:
                for k, v in article.entities.items():
                    if k not in all_entities:
                        all_entities[k] = []
                    all_entities[k].extend(v)

    if not location_votes:
        return None

    # Pick most voted location
    best_key = max(location_votes, key=lambda k: location_votes[k]["count"])
    best_location = location_votes[best_key]["data"]

    # Calculate confidence based on vote concentration
    total_votes = sum(v["count"] for v in location_votes.values())
    best_votes = location_votes[best_key]["count"]
    location_confidence = min(best_votes / max(total_votes, 1) * 1.2, 1.0)

    # Create event
    event = Event(
        story_id=story_id,
        latitude=best_location["latitude"],
        longitude=best_location["longitude"],
        location_name=best_location.get("location_name"),
        location_type=best_location.get("location_type"),
        radius_km=best_location.get("radius_km"),
        start_time=best_location.get("event_time") or datetime.now(timezone.utc),
        event_type=best_location.get("event_type", "other"),
        confidence=location_confidence,
        source_count=total_votes,
        entities=all_entities,
    )

    return {
        "event": event,
        "confidence": location_confidence,
        "votes": best_votes,
        "total_votes": total_votes,
    }


async def enrich_story_with_event(
    session,
    story_id: str,
) -> bool:
    """
    Enrich a story with an Event if it doesn't have one.

    Returns:
        True if event was created
    """
    from sqlalchemy import select
    from src.schema.models import Event

    # Check if event already exists
    stmt = select(Event).where(Event.story_id == story_id)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        return False

    result = await create_event_from_story(session, story_id)
    if result and result.get("event"):
        session.add(result["event"])
        await session.commit()
        return True

    return False