"""Claim extraction: populate claims + claim_evidence for gated stories.

One LLM call per story (not per unit) -- see AGENT_TASKS.md P2-B for why.
"""
import logging
import uuid
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from src.schema.models import Story, Claim, ClaimEvidence, ClaimType, ClaimStance
from src.verification.stories import _gather_unit_texts_for_story
from src.shared.analyzer_versions import (
    CLAIM_VERSION,
    compute_input_hash,
    hash_id_set,
    hash_text,
)

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

# The model's ceiling for one story's claim matrix, and the number of units folded
# into the prompt. Both are measurements, not preferences: see the call site below.
# Defaults are 4096 tokens and 5 units, which measured 2,437 total tokens per call
# against Groq's published 8,000 tokens/minute for this model. The stage was
# originally written at 10 units and 1,500 tokens, which measured 4,962 tokens per
# call -- 62% of a minute's allowance for one story, and short enough that the
# model could answer entirely inside its reasoning.
CLAIM_MAX_TOKENS = 4096
CLAIM_MAX_UNITS = 5

# The smallest story worth an LLM call. Two is not a tuning choice: a one-unit
# story is one source's account of one event, and asking a model to find the
# atomic claims and their corroboration across a single source produces a matrix
# with one column -- which is the article, not an extraction of it.
MIN_CLAIM_UNITS = 2


def story_input_digest(unit_ids) -> str:
    """Digest of the evidence a claim matrix for one story was computed from.

    Sorted and de-duplicated (see analyzer_versions.hash_id_set), so the same set of
    units reads the same however it was assembled and a re-run over unchanged
    evidence is verifiably a re-run rather than new state.
    """
    return hash_id_set(CLAIM_VERSION, unit_ids)


def claim_input_hash(story_id, unit_digest: str, claim_text: str) -> str:
    """The input_hash a claim row carries.

    Three inputs, not one. A claim is not a function of the story alone: it is a
    reading of the story *as its units stood when the reading was taken*. Hashing
    (story, claim_text) alone -- which is what this did before -- makes a claim
    look current after the evidence under it changed, and leaves the caller with
    no way to ask "has this story been read already?" without a second heuristic.
    With the unit digest folded in, the stored hash is exactly re-derivable from
    data the database already holds, which is what src/verification/phase2.py uses
    to decide whether a story would re-spend.

    Whitespace and case are normalized inside hash_text: the same claim re-worded
    only in spacing or capitalization is the same claim, and a re-run that returned
    it slightly differently must not look like new state.
    """
    return hash_text(CLAIM_VERSION, story_id, unit_digest, claim_text)


async def claims_are_current(
    session: AsyncSession, story_id: uuid.UUID, unit_digest: str,
) -> bool:
    """True when this story already carries a claim matrix for `unit_digest`.

    The pre-spend half of the recomputable-derived-state contract. It re-derives
    each stored claim's hash from the claim text that is already in the row and
    compares it, rather than asking "are there any claims?" -- a row count cannot
    distinguish "read once and unchanged" from "read once against different
    evidence", and answering the second question wrongly is how a daily job spends
    the same token budget twice.

    False means the extraction should run: either no claims, or claims whose hash
    no longer matches the evidence in front of the story.
    """
    stmt = select(Claim.text, Claim.input_hash, Claim.analyzer_version).where(
        Claim.story_id == story_id
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return False
    for text, stored_hash, version in rows:
        if version != CLAIM_VERSION:
            return False
        if stored_hash != claim_input_hash(story_id, unit_digest, text):
            return False
    return True


async def story_evidence_snapshot(
    session: AsyncSession, story_id: uuid.UUID, *, max_units: int = CLAIM_MAX_UNITS,
) -> tuple[str, int]:
    """What an extraction for this story would be taken against: (digest, unit count).

    Both in one gather because the caller needs both before it spends anything: the
    digest to ask claims_are_current() whether the story has been read, and the
    count to decide whether it is worth reading at all. Gathering twice would
    mean two chances for the two answers to disagree.

    The digest is over unit ids, so it is stable regardless of how the units are
    ordered; the count is of units that actually carry article text, because that
    is what the prompt would be built from. A story id that does not exist
    returns ("", 0) rather than raising: a story deleted between selection and
    here is a skip, not a crash.
    """
    stmt = select(Story).where(Story.id == story_id)
    story = (await session.execute(stmt)).scalar_one_or_none()
    if not story:
        return "", 0
    unit_texts = await _gather_unit_texts_for_story(session, story, max_units=max_units)
    return story_input_digest(u["unit_id"] for u in unit_texts), len(unit_texts)


async def story_unit_digest(
    session: AsyncSession, story_id: uuid.UUID, *, max_units: int = CLAIM_MAX_UNITS,
) -> str:
    """The digest half of story_evidence_snapshot().

    Kept as its own name for the tests and for any caller that only needs the
    "already read?" answer.
    """
    digest, _count = await story_evidence_snapshot(session, story_id, max_units=max_units)
    return digest


def _parse_claim_rows(
    data: dict, *, story_id: uuid.UUID, unit_digest: str,
) -> list[tuple[dict, list[dict]]]:
    """The model's answer as plain dicts, with nothing sent to the database.

    Split out of the writer so that a malformed evidence row, a bad claim_type, a
    truncated JSON body -- everything that can go wrong between the provider's
    answer and a commit -- is decided before the connection is touched, and so
    that the write can be replayed on a new connection without a second LLM call.
    The rows carry the input_hash each one needs so the digest computed from the
    evidence is not recomputed per row and cannot drift between the two paths.
    """
    rows = []
    for claim_data in data.get("claims", []):
        claim_text = claim_data["text"]
        claim = {
            "story_id": story_id,
            "text": claim_text,
            "claim_type": ClaimType(claim_data.get("claim_type", "fact")),
            # A claim's input is the story it was extracted from plus the claim text
            # itself, normalized: the same claim re-worded only in whitespace or case is
            # the same claim, and a re-run that returned it slightly differently must not
            # look like new state.
            "analyzer_version": CLAIM_VERSION,
            "input_hash": claim_input_hash(story_id, unit_digest, claim_text),
        }
        evidence = []
        for ev in claim_data.get("evidence", []):
            try:
                evidence.append({
                    "unit_id": uuid.UUID(ev["unit_id"]),
                    "stance": ClaimStance(ev.get("stance", "neutral")),
                    "confidence": int(ev.get("confidence", 50)),
                })
            except (KeyError, ValueError, TypeError):
                # A malformed evidence row shouldn't drop the whole claim, and
                # saying which claim lost it is the difference between a debuggable
                # log and "some evidence went missing".
                logger.warning("Skipping malformed evidence for claim %r", claim_text[:80])
        rows.append((claim, evidence))
    return rows


def _is_disconnect(error: BaseException) -> bool:
    """Did the connection die, as opposed to the statement being wrong?

    Narrow on purpose. A unique-violation or a bad column is a bug or a
    duplicate, and retrying it on a new connection would fail identically while
    hiding the first error. A closed connection, a dropped socket, or a server
    that went away are the cases where the identical statement on a fresh
    connection can succeed.
    """
    if isinstance(error, (InterfaceError, OperationalError)):
        return True
    text = str(error).lower()
    # asyncpg's own wording when the socket dies mid-query is "server closed the
    # connection unexpectedly", which arrives wrapped rather than as an
    # InterfaceError when it happens outside SQLAlchemy's execution path.
    return "connection is closed" in text or "closed the connection" in text


async def _write_claim_rows(
    session: AsyncSession,
    rows: list[tuple[dict, list[dict]]],
    *,
    sessionmaker=None,
) -> dict:
    """Write parsed claim rows, reconnecting once if the connection died.

    The retry is here, around the write only, and never around the LLM call.
    That split is the whole reason the parse is a separate step: measured on dev
    2026-10-03, a Phase 2 story spent a real LLM call and then lost the entire
    result to `InterfaceError: connection is closed` at the flush, because the
    call took long enough (the run paces calls 25s apart) for the connection
    held open by the session to go stale underneath it. Retrying the stage would
    have bought a second call's tokens for the same answer; retrying the write
    buys the first call's answer for free.

    Without a sessionmaker the write is attempted once and the error propagates,
    which is what the pre-existing callers (scripts/run_claim_extraction.py, the
    recompute path) get -- they open a session per story and have no reason to
    hand one over.
    """
    counts = {"claims_created": 0, "evidence_created": 0}
    try:
        await _insert_claim_rows(session, rows, counts)
    except Exception as first:
        if sessionmaker is None or not _is_disconnect(first):
            raise
        logger.warning(
            "Claim write lost its connection (%s); retrying once on a fresh one",
            type(first).__name__,
        )
        await session.rollback()
        async with sessionmaker() as fresh:
            await _insert_claim_rows(fresh, rows, counts)
    return counts


async def _insert_claim_rows(
    session: AsyncSession,
    rows: list[tuple[dict, list[dict]]],
    counts: dict,
) -> None:
    """Insert one story's claims and their evidence, and commit."""
    for claim_row, evidence_rows in rows:
        claim = Claim(**claim_row)
        session.add(claim)
        await session.flush()  # get claim.id before adding evidence rows
        counts["claims_created"] += 1

        for ev in evidence_rows:
            session.add(ClaimEvidence(
                claim_id=claim.id,
                unit_id=ev["unit_id"],
                stance=ev["stance"],
                confidence=ev["confidence"],
                # One evidence row is one (claim, unit) stance report -- the same key
                # as uq_claim_unit -- plus the stance itself, which is the judgement
                # the row records. The confidence is a number attached to that
                # judgement, not an input to it.
                analyzer_version=CLAIM_VERSION,
                input_hash=compute_input_hash(
                    CLAIM_VERSION, str(claim.id), str(ev["unit_id"]), ev["stance"].value
                ),
            ))
            counts["evidence_created"] += 1

    await session.commit()


async def extract_claims_for_story(
    session: AsyncSession,
    story_id: uuid.UUID,
    *,
    max_units: int = CLAIM_MAX_UNITS,
    budget=None,
    sessionmaker=None,
) -> dict:
    """Extract and persist the claim matrix for one story.

    Returns a result dict with counts, matching the shape enrich_story()
    in src/enrichment/pipeline.py uses (errors: list, plus per-type counts)
    so this slots into the same reporting pattern as weekly enrichment.

    `budget`, when given, is a src.verification.phase2.Phase2TokenBudget and is
    consulted immediately before the LLM call and charged immediately after it.
    It is a parameter rather than an import so this function stays usable without
    a budget row (scripts/run_claim_extraction.py, the recompute path) and so the
    budget concern is visible in the signature of the one function that spends.
    A budget refusal is re-raised rather than recorded as an error: the caller
    needs to stop working through the queue, which an errors list cannot express.

    `sessionmaker` is used for one thing only: re-establishing the database
    connection if it dies between the LLM call and the insert. See
    _write_claim_rows for why that costs the caller nothing to accept.
    """
    results = {"story_id": str(story_id), "claims_created": 0, "evidence_created": 0, "errors": []}

    stmt = select(Story).where(Story.id == story_id)
    story = (await session.execute(stmt)).scalar_one_or_none()
    if not story:
        results["errors"].append("Story not found")
        return results

    unit_texts = await _gather_unit_texts_for_story(session, story, max_units=max_units)
    if len(unit_texts) < MIN_CLAIM_UNITS:
        # Not enough units for a meaningful claim matrix -- same threshold
        # philosophy as cluster_viewpoints, doesn't need to match exactly.
        return results

    texts_for_prompt = "\n\n---\n\n".join(
        f"unit_id: {u['unit_id']}\nSource: {u['source_tier']}\n{u['text']}" for u in unit_texts
    )
    # The evidence this reading is taken against. Folded into every claim's
    # input_hash so claims_are_current() can tell "already read, unchanged" from
    # "read against different evidence" without spending anything.
    unit_digest = story_input_digest(u["unit_id"] for u in unit_texts)

    from src.shared.llm import get_llm_client
    llm = await get_llm_client()

    prompt = CLAIM_EXTRACTION_PROMPT.format(texts_for_prompt=texts_for_prompt)

    if budget is not None:
        # Before the call, never after: this is the gate. Phase2BudgetRefused
        # propagates out of the try below untouched (see the except clause).
        await budget.ensure_headroom(prompt)
        await budget.pace()

    try:
        result = await llm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            # max_tokens and reasoning_effort are one decision, not two. The model
            # this pipeline is configured against (openai/gpt-oss-20b) spends its
            # completion budget on its own reasoning first: measured live on
            # 2026-10-03, max_tokens=1500 with no reasoning_effort returned
            # finish_reason="length" and, on a second identical call, an EMPTY
            # message content -- a stage that costs a request and produces nothing.
            # 4096 with reasoning_effort="low" returned finish_reason="stop" and
            # complete JSON on every call. Groq rejects the literal "none", so
            # "low" is the floor available, not "off".
            max_tokens=CLAIM_MAX_TOKENS,
            reasoning_effort="low",
        )
        content = result["choices"][0]["message"]["content"]
        if budget is not None:
            # Charged here, immediately after the call and before anything can
            # return early, because the tokens are spent by then whatever the
            # caller goes on to do with the result. Real usage, not the estimate
            # the gate used: the counter should track spend, and record() floors
            # at 1 so a call whose usage is missing is still counted.
            await budget.record((result.get("usage") or {}).get("total_tokens", 0))
        if not (content or "").strip():
            # An empty completion is the reasoning-model failure above, and the
            # bare `except Exception` below would turn it into a silent zero. Say
            # which of the two it was so the log can be read rather than guessed.
            results["errors"].append(
                f"empty completion (finish_reason={result.get('finish_reason')!r}, "
                f"usage={result.get('usage')})"
            )
            return results
        # Use llm._parse_json_response, not raw json.loads -- it strips
        # fenced code blocks. cluster_viewpoints uses raw json.loads and is
        # fragile to this; don't repeat that in new code.
        data = llm._parse_json_response(content)
        rows = _parse_claim_rows(data, story_id=story_id, unit_digest=unit_digest)
        written = await _write_claim_rows(
            session, rows, sessionmaker=sessionmaker,
        )
        results["claims_created"] = written["claims_created"]
        results["evidence_created"] = written["evidence_created"]
    except Exception as e:
        if budget is not None:
            from src.verification.phase2 import Phase2BudgetRefused

            if isinstance(e, Phase2BudgetRefused):
                # Not this function's error to swallow: the caller must stop
                # walking the queue, and a budget refusal is a decision, not a
                # failure of this story.
                raise
        logger.warning(f"Claim extraction failed for story {story_id}: {e}", exc_info=True)
        results["errors"].append(str(e))

    return results


async def extract_claims_for_recent_stories(
    session_factory, hours_back: int = 168, max_stories: int = 100, budget=None,
) -> list[dict]:
    """Batch entry point -- mirrors enrich_recent_stories() in
    src/enrichment/pipeline.py.

    Retargeted for Phase 2 (2026-10-03): the pool used to be QUEUED-only, which
    on the dev database selected nothing (0 QUEUED stories existed), so claim
    extraction had never produced a row. PENDING stories have already passed
    the dynamic gate, so they are safe to spend on. Public exposure is a
    separate, human-gated decision and is untouched by this change.

    `budget` defaults to a real Phase2TokenBudget rather than to None, and that
    default is the point. This function is also what the pre-existing weekly job
    calls, and a default of "no budget" would leave that path -- now pointed at
    PENDING stories, i.e. a non-empty pool -- spending with no cap at all. An
    unbounded default on a function that spends money is the wrong default even
    when the caller happens to pass one.
    """
    from src.verification.phase2 import (
        Phase2BudgetRefused,
        Phase2TokenBudget,
        select_phase2_stories,
    )

    if budget is None:
        budget = Phase2TokenBudget()

    async with session_factory() as session:
        stories = await select_phase2_stories(
            session, hours_back=hours_back, max_stories=max_stories
        )

    story_ids = [str(s.id) for s in stories]

    if not story_ids:
        logger.info("No gate-passed stories to extract claims for")
        return []

    logger.info(f"Extracting claims for {len(story_ids)} gate-passed stories")

    # Process each story in its own session (like enrich_stories_batch)
    results = []
    for story_id in story_ids:
        try:
            async with session_factory() as session:
                result = await extract_claims_for_story(
                    session, uuid.UUID(story_id), max_units=CLAIM_MAX_UNITS,
                    budget=budget,
                )
                results.append(result)
        except Phase2BudgetRefused as exc:
            # Stop walking the queue, do not fail: the remaining stories are
            # tomorrow's problem, and they are still selected newest-first.
            logger.info(
                "Phase 2 token allowance spent after %d/%d stories (%d remaining): %s",
                len(results), len(story_ids), len(story_ids) - len(results), exc,
            )
            break
    return results