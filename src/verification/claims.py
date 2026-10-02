"""Claim extraction: populate claims + claim_evidence for gated stories.

One LLM call per story (not per unit) -- see AGENT_TASKS.md P2-B for why.
"""
import logging
import uuid
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import Story, Claim, ClaimEvidence, ClaimType, ClaimStance
from src.verification.stories import _gather_unit_texts_for_story
from src.shared.analyzer_versions import CLAIM_VERSION, compute_input_hash, hash_text

logger = logging.getLogger(__name__)

CLAIM_EXTRACTION_PROMPT = """Analyze these news article excerpts about the same event and extract the atomic factual claims being made.

For each distinct claim, identify:
- The claim text (one sentence, self-contained)
- claim_type: one of "fact", "allegation", "prediction", "quote"
- Which sources support it, dispute it, or are neutral/don't mention it

Articles:
{texts_for_prompt}

Return ONLY a JSON object of this exact shape, no explanation, no markdown fences:
{{
  "claims": [
    {{
      "text": "...",
      "claim_type": "fact",
      "evidence": [
        {{"unit_id": "...", "stance": "supports", "confidence": 80}}
      ]
    }}
  ]
}}
"""


async def extract_claims_for_story(session: AsyncSession, story_id: uuid.UUID) -> dict:
    """Extract and persist the claim matrix for one story.

    Returns a result dict with counts, matching the shape enrich_story()
    in src/enrichment/pipeline.py uses (errors: list, plus per-type counts)
    so this slots into the same reporting pattern as weekly enrichment.
    """
    results = {"story_id": str(story_id), "claims_created": 0, "evidence_created": 0, "errors": []}

    stmt = select(Story).where(Story.id == story_id)
    story = (await session.execute(stmt)).scalar_one_or_none()
    if not story:
        results["errors"].append("Story not found")
        return results

    unit_texts = await _gather_unit_texts_for_story(session, story, max_units=10)
    if len(unit_texts) < 2:
        # Not enough units for a meaningful claim matrix -- same threshold
        # philosophy as cluster_viewpoints, doesn't need to match exactly.
        return results

    texts_for_prompt = "\n\n---\n\n".join(
        f"unit_id: {u['unit_id']}\nSource: {u['source_tier']}\n{u['text']}" for u in unit_texts
    )

    from src.shared.llm import get_llm_client
    llm = await get_llm_client()

    try:
        result = await llm.chat_completion(
            messages=[{"role": "user", "content": CLAIM_EXTRACTION_PROMPT.format(texts_for_prompt=texts_for_prompt)}],
            temperature=0.1,
            max_tokens=1500,
        )
        # Use llm._parse_json_response, not raw json.loads -- it strips
        # fenced code blocks. cluster_viewpoints uses raw json.loads and is
        # fragile to this; don't repeat that in new code.
        data = llm._parse_json_response(result["choices"][0]["message"]["content"])

        for claim_data in data.get("claims", []):
            claim_text = claim_data["text"]
            claim = Claim(
                story_id=story_id,
                text=claim_text,
                claim_type=ClaimType(claim_data.get("claim_type", "fact")),
                # A claim's input is the story it was extracted from plus the claim text
                # itself, normalized: the same claim re-worded only in whitespace or case is
                # the same claim, and a re-run that returned it slightly differently must not
                # look like new state.
                analyzer_version=CLAIM_VERSION,
                input_hash=hash_text(CLAIM_VERSION, story_id, claim_text),
            )
            session.add(claim)
            await session.flush()  # get claim.id before adding evidence rows
            results["claims_created"] += 1

            for ev in claim_data.get("evidence", []):
                try:
                    stance = ClaimStance(ev.get("stance", "neutral"))
                    evidence = ClaimEvidence(
                        claim_id=claim.id,
                        unit_id=uuid.UUID(ev["unit_id"]),
                        stance=stance,
                        confidence=int(ev.get("confidence", 50)),
                        # One evidence row is one (claim, unit) stance report -- the same key
                        # as uq_claim_unit -- plus the stance itself, which is the judgement
                        # the row records. The confidence is a number attached to that
                        # judgement, not an input to it.
                        analyzer_version=CLAIM_VERSION,
                        input_hash=compute_input_hash(
                            CLAIM_VERSION, str(claim.id), str(ev["unit_id"]), stance.value
                        ),
                    )
                    session.add(evidence)
                    results["evidence_created"] += 1
                except (KeyError, ValueError) as e:
                    # A malformed evidence row shouldn't drop the whole claim
                    logger.warning(f"Skipping malformed evidence for claim {claim.id}: {e}")

        await session.commit()
    except Exception as e:
        logger.warning(f"Claim extraction failed for story {story_id}: {e}", exc_info=True)
        results["errors"].append(str(e))

    return results


async def extract_claims_for_recent_stories(
    session_factory, hours_back: int = 168, max_stories: int = 100,
) -> list[dict]:
    """Batch entry point -- mirrors enrich_recent_stories() in
    src/enrichment/pipeline.py. Only processes QUEUED stories (see P2-B goal:
    don't spend budget on stories that never got past the gate)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    # Get recent QUEUED stories - use a temporary session for this query
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

    story_ids = [str(s.id) for s in stories]

    if not story_ids:
        logger.info("No QUEUED stories to extract claims for")
        return []

    logger.info(f"Extracting claims for {len(story_ids)} QUEUED stories")

    # Process each story in its own session (like enrich_stories_batch)
    results = []
    for story_id in story_ids:
        async with session_factory() as session:
            result = await extract_claims_for_story(session, uuid.UUID(story_id))
            results.append(result)

    return results