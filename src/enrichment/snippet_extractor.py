"""Snippet extraction from articles using LLM for key quotes, stats, facts."""

import re
from typing import Any
import logging

from src.shared.llm import get_llm_client
from src.utils.minhash_utils import shingle_text
from src.schema.models import Snippet, Snippet as SnippetType
from src.utils.ingest_stats import STATS

logger = logging.getLogger(__name__)

# Completion budget for one snippet extraction. 1500 truncated 12 of 30 corpus
# articles to nothing (batch-evalset findings): six hit the cap mid-string and
# six were reasoning models that spent the whole budget on reasoning and
# returned no answer at all. 3000 measured 30/30 parseable on the same corpus
# and 4000 added nothing, so this is the smallest cap that stops losing output.
SNIPPET_MAX_TOKENS = 3000


# The five values Snippet.snippet_type's enum accepts (schema/models.py:502). The model
# is asked for exactly these, but a free-form completion can invent one, and
# enrich_story_with_snippets does SnippetType(s["snippet_type"]) unguarded -- an unknown
# type raises ValueError inside the story's persistence, after the extraction succeeded.
VALID_SNIPPET_TYPES = ("quote", "stat", "fact", "summary", "claim")
DEFAULT_SNIPPET_TYPE = "fact"


def _coerce_confidence(value: Any) -> int:
    """Best-effort 0-100 int. Models answer with "high", 88.4, None or a list."""
    try:
        return min(100, max(0, int(float(value))))
    except (TypeError, ValueError):
        return 80


def _coerce_position(value: Any) -> int:
    """Best-effort 0..1 float -> integer character position."""
    try:
        return int(float(value) * 1000000)
    except (TypeError, ValueError):
        return 500000


def _coerce_entities(value: Any) -> list:
    if isinstance(value, list):
        return [e for e in value if isinstance(e, str)]
    return []


def _record(source: str, event: str, n: int = 1) -> None:
    """STATS.record, but a counter can never destroy a good extraction.

    `record(source, event, n=1)` raised TypeError inside the extraction try
    block on a `count=` keyword, and the fail-soft handler turned a successful
    extraction into []. Bookkeeping must not be able to do that.
    """
    try:
        STATS.record(source, event, n)
    except Exception as exc:  # pragma: no cover - counters are best effort
        logger.warning(f"ingest stats unavailable ({type(exc).__name__}), continuing")


async def extract_snippets_from_article(
    article_id: str,
    story_id: str,
    text: str,
    title: str,
    max_snippets: int = 5,
) -> list[dict[str, Any]]:
    """
    Extract key snippets (quotes, stats, facts) from an article using LLM.

    Args:
        article_id: Source article UUID
        story_id: Parent story UUID
        text: Article body text
        title: Article title
        max_snippets: Maximum snippets to extract

    Returns:
        List of snippet dicts ready for Snippet model
    """
    if not text or len(text) < 200:
        return []

    # Truncate for LLM context
    text_for_llm = text[:8000]

    llm = await get_llm_client()

    prompt = f"""Extract the most important snippets from this news article. Focus on:
1. Direct quotes from named sources
2. Key statistics/numbers with context
3. Important factual claims
4. Definitive statements about events

Article Title: {title}
Article Text: {text_for_llm}

Return a JSON array of snippet objects with these fields:
- "text": The exact snippet text (max 300 chars)
- "type": One of "quote", "stat", "fact", "summary", "claim"
- "entities": Array of entity names mentioned in snippet
- "confidence": 0-100 confidence this is a key snippet
- "position_estimate": Approximate position in article (0=start, 1=end)

Only return the JSON array, no explanation. Maximum {max_snippets} snippets."""

    try:
        response = await llm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=SNIPPET_MAX_TOKENS,
        )
        content = (response["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        # Provider/transport failure, or a client that is not what we think it is.
        # Say which: an un-awaited get_llm_client() used to land here as
        # AttributeError: 'coroutine' object has no attribute 'chat' and look
        # identical to a quota error.
        logger.error(
            f"Snippet extraction provider failure for article {article_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        _record("snippets", f"extraction_failed:error_{type(exc).__name__}")
        return []

    if not content:
        # A reasoning model can spend the whole completion budget on reasoning
        # and emit no answer. That is a different failure from a provider error
        # and from a truncated response, so it gets its own event.
        logger.warning(
            f"Snippet extraction got empty content for article {article_id} "
            f"(max_tokens={SNIPPET_MAX_TOKENS})"
        )
        _record("snippets", "empty_response")
        return []

    # _parse_json_response strips a ``` fence before json.loads; raw json.loads
    # does not (see the note in src/verification/claims.py).
    try:
        snippets_data = llm._parse_json_response(content)
    except Exception as exc:
        logger.warning(
            f"Failed to parse snippet JSON for article {article_id}: "
            f"{type(exc).__name__}: {exc}; content={content[:200]!r}"
        )
        _record("snippets", f"parse_failed:error_{type(exc).__name__}")
        return []

    if isinstance(snippets_data, dict):
        # Some models wrap the array in an object; accept the obvious shapes
        # rather than dropping a good extraction on a technicality.
        for key in ("snippets", "results", "items", "data"):
            if isinstance(snippets_data.get(key), list):
                snippets_data = snippets_data[key]
                break
        else:
            logger.warning(
                f"snippet JSON for article {article_id} is an object with no "
                f"snippet array (keys={sorted(snippets_data)})"
            )
            _record("snippets", "parse_failed:not_a_list")
            return []
    if not isinstance(snippets_data, list):
        logger.warning(
            f"snippet JSON for article {article_id} is "
            f"{type(snippets_data).__name__}, expected a list"
        )
        _record("snippets", "parse_failed:not_a_list")
        return []

    # Process and validate snippets.
    #
    # Every coercion below is defensive on purpose. This loop deliberately sits OUTSIDE
    # the try blocks above (each failure mode needs its own event), which means an
    # un-guarded int()/strip() here would raise straight out of the function -- and
    # extract_snippets_for_story has no per-article guard, so one malformed field from
    # one article would discard every other article's snippets for that story.
    processed_snippets = []
    for s in snippets_data[:max_snippets]:
        if not isinstance(s, dict):
            continue

        raw_text = s.get("text")
        if not isinstance(raw_text, str):
            continue
        snippet_text = raw_text.strip()
        if len(snippet_text) < 20:
            continue

        # Truncate if too long
        if len(snippet_text) > 300:
            snippet_text = snippet_text[:297] + "..."

        snippet_type = s.get("type")
        if snippet_type not in VALID_SNIPPET_TYPES:
            if isinstance(snippet_type, str):
                logger.warning(
                    f"snippet type {snippet_type!r} is not one of "
                    f"{VALID_SNIPPET_TYPES}; storing as {DEFAULT_SNIPPET_TYPE}"
                )
            snippet_type = DEFAULT_SNIPPET_TYPE

        processed_snippets.append({
            "article_id": article_id,
            "story_id": story_id,
            "snippet_type": snippet_type,
            "text": snippet_text,
            "entities": _coerce_entities(s.get("entities")),
            # shingle_text returns a Set[str], and Snippet.minhash_signature is a JSON
            # column (models.py:516) -- so handing it the raw set makes the INSERT fail
            # with "Object of type set is not JSON serializable". That killed every write
            # even once the un-awaited client was fixed. sorted() keeps it deterministic,
            # which matters because this value is a dedup input.
            "minhash_signature": sorted(shingle_text(snippet_text, k=5)),
            "confidence": _coerce_confidence(s.get("confidence", 80)),
            "position": _coerce_position(s.get("position_estimate", 0.5)),
        })

    _record("snippets", "extracted", len(processed_snippets))
    if not processed_snippets:
        # The response parsed and had entries, but none survived validation. That is a
        # different problem from a parse failure and worth its own count.
        _record("snippets", "no_valid_snippets")
    return processed_snippets


async def extract_snippets_for_story(
    story_id: str,
    articles: list[dict[str, Any]],
    max_per_article: int = 3,
) -> list[dict[str, Any]]:
    """
    Extract snippets from all articles in a story.

    Args:
        story_id: Story UUID
        articles: List of dicts with article_id, title, body_text
        max_per_article: Max snippets per article

    Returns:
        Combined list of snippets
    """
    all_snippets = []

    for article in articles:
        article_id = article.get("article_id") or article.get("id")
        title = article.get("title", "")
        body_text = article.get("body_text", "")

        if not body_text:
            continue

        snippets = await extract_snippets_from_article(
            article_id=article_id,
            story_id=story_id,
            text=body_text,
            title=title,
            max_snippets=max_per_article,
        )

        all_snippets.extend(snippets)

    # Deduplicate by minhash
    all_snippets = deduplicate_snippets(all_snippets)

    return all_snippets


def deduplicate_snippets(snippets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Remove duplicate snippets using MinHash similarity.

    Keeps the one with highest confidence.
    """
    if not snippets:
        return []

    # Sort by confidence descending
    snippets = sorted(snippets, key=lambda s: s.get("confidence", 0), reverse=True)

    unique = []
    for snippet in snippets:
        sig = snippet.get("minhash_signature")
        if not sig:
            unique.append(snippet)
            continue

        is_duplicate = False
        for existing in unique:
            existing_sig = existing.get("minhash_signature")
            if existing_sig and sig:
                # Quick containment check
                if set(sig).intersection(set(existing_sig)):
                    is_duplicate = True
                    break

        if not is_duplicate:
            unique.append(snippet)

    return unique


async def extract_key_quotes(
    text: str,
    max_quotes: int = 5,
) -> list[str]:
    """
    Extract direct quotes from text using regex patterns.

    Returns list of quote strings.
    """
    # Pattern for quoted speech
    quote_patterns = [
        r'"([^"]{20,300})"',  # Double quotes
        r"'([^']{20,300})'",  # Single quotes
        r'[\u201c\u201d]([^\u201c\u201d]{20,300})[\u201c\u201d]',  # Smart quotes
    ]

    quotes = []
    for pattern in quote_patterns:
        matches = re.findall(pattern, text)
        for match in matches:
            match = match.strip()
            if len(match) >= 20 and len(match) <= 300:
                # Filter out non-quote content (titles, etc.)
                if not re.match(r'^[A-Z][a-z]+ [A-Z][a-z]+$', match):  # Not just a name
                    quotes.append(match)

    # Deduplicate and limit
    seen = set()
    unique_quotes = []
    for q in quotes:
        if q not in seen:
            seen.add(q)
            unique_quotes.append(q)

    return unique_quotes[:max_quotes]


async def extract_statistics(text: str, max_stats: int = 5) -> list[dict[str, Any]]:
    """
    Extract statistical claims from text.

    Returns list of dicts with stat text and context.
    """
    # Patterns for statistics
    stat_patterns = [
        r'(\d+(?:,\d{3})*(?:\.\d+)?%?)\s+(?:of|out of|percent|percentage)',
        r'(?:increased|decreased|rose|fell|grew|dropped)\s+(?:by\s+)?(\d+(?:\.\d+)?%?)',
        r'(?:more than|less than|over|under|nearly|about|approximately)\s+(\d+(?:,\d{3})*(?:\.\d+)?)\s+(?:million|billion|trillion|thousand|people|percent)',
        r'(\d+(?:\.\d+)?)\s*(?:percent|%)\s+(?:of|in|from)',
    ]

    stats = []
    for pattern in stat_patterns:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for match in matches:
            # Get context around match
            start = max(0, match.start() - 100)
            end = min(len(text), match.end() + 100)
            context = text[start:end].strip()

            stats.append({
                "stat_text": match.group(0),
                "context": context,
                "position": match.start(),
            })

    # Deduplicate
    seen = set()
    unique_stats = []
    for s in stats:
        if s["stat_text"] not in seen:
            seen.add(s["stat_text"])
            unique_stats.append(s)

    return unique_stats[:max_stats]


async def extract_factual_claims(
    text: str,
    max_claims: int = 5,
) -> list[str]:
    """
    Extract definitive factual claims from text.

    Returns list of claim strings.
    """
    # Pattern for factual assertions
    claim_patterns = [
        r'(?:confirmed|announced|declared|stated|revealed|disclosed)\s+that\s+([^.]{30,300})',
        r'(?:according to|sources say|officials said|report says)\s+([^.]{30,300})',
        r'(?:will|is set to|is expected to|plans to)\s+([^.]{30,300})',
    ]

    claims = []
    for pattern in claim_patterns:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for match in matches:
            claim = match.group(1).strip()
            if len(claim) >= 30 and len(claim) <= 300:
                claims.append(claim)

    # Deduplicate
    seen = set()
    unique_claims = []
    for c in claims:
        if c not in seen:
            seen.add(c)
            unique_claims.append(c)

    return unique_claims[:max_claims]


async def enrich_story_with_snippets(
    session,
    story_id: str,
    max_snippets_per_article: int = 3,
) -> int:
    """
    Enrich a story with extracted snippets.

    Args:
        session: Database session
        story_id: Story UUID
        max_snippets_per_article: Max snippets per article

    Returns:
        Number of snippets created
    """
    from sqlalchemy import select
    from src.schema.models import RawArticle, StoryUnitLink, ReportingUnit

    # Get all articles in story
    stmt = (
        select(RawArticle)
        .join(ReportingUnit, RawArticle.id == ReportingUnit.representative_article_id)
        .join(StoryUnitLink, StoryUnitLink.unit_id == ReportingUnit.id)
        .where(StoryUnitLink.story_id == story_id)
    )
    result = await session.execute(stmt)
    articles = result.scalars().all()

    if not articles:
        return 0

    # Prepare article data
    article_data = []
    for article in articles:
        article_data.append({
            "article_id": article.id,
            "title": article.title,
            "body_text": article.body_text,
        })

    # Extract snippets
    snippets = await extract_snippets_for_story(
        story_id=story_id,
        articles=article_data,
        max_per_article=max_snippets_per_article,
    )

    # Store snippets
    count = 0
    for s in snippets:
        snippet = Snippet(
            story_id=s["story_id"],
            article_id=s["article_id"],
            snippet_type=SnippetType.SnippetType(s["snippet_type"]),
            text=s["text"],
            entities=s["entities"],
            minhash_signature=s["minhash_signature"],
            confidence=s["confidence"],
            position=s["position"],
        )
        session.add(snippet)
        count += 1

    if count > 0:
        await session.commit()

    return count