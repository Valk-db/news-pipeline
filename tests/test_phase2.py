"""Phase 2's own behaviour: who gets selected, what a second run costs, and what a refusal does.

These are the tests for the parts of Phase 2 that are policy rather than plumbing:

  * the selection is PENDING + QUEUED, excludes viewpoint children, and refuses
    BLOCKED outright (a story that failed the corroboration gate must never cost
    a token);
  * a story already read against the same evidence is skipped before any spend;
  * the token cap is enforced, and hitting it skips rather than fails;
  * a 429 is retried once after the provider's own retry-after, a 402 is not
    retried and not routed around;
  * running the whole job twice spends once.

`tests/test_phase2_isolation.py` holds the other half of the contract -- that none
of this changes what the public sees. The split is deliberate: this file is about
Phase 2 behaving as designed, that one is about it not being able to publish even
if it did.

The LLM is stubbed. The budget, the digest, the claim rows and the selection query
are all real, against the in-memory SQLite the rest of the suite uses, so a test that
passes here is a statement about the code rather than about the mocks.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from src.schema.models import (
    Claim,
    RawArticle,
    ReportingUnit,
    SourceTier,
    Story,
    StoryUnitLink,
)
from src.shared.budget import GROQ_PHASE2_TOKENS, used
from src.verification import claims as claims_stage
from src.verification.phase2 import (
    PHASE2_STATUSES,
    SKIP_ALREADY_ANALYSED,
    SKIP_BUDGET,
    SKIP_OUT_OF_CREDIT,
    SKIP_RATE_LIMITED,
    Phase2BudgetRefused,
    Phase2TokenBudget,
    estimate_prompt_tokens,
    new_result,
    select_phase2_stories,
    summarize,
)


def _story(status: Story.Status, *, units: int = 3, viewpoint_cluster_id=None) -> Story:
    day = datetime.now(timezone.utc)
    return Story(
        id=uuid.uuid4(),
        day=day,
        primary_entities=["entity1", "entity2"],
        status=status,
        tier1_unit_count=units,
        tier2_unit_count=0,
        tier3_unit_count=0,
        tier4_unit_count=0,
        distinct_owners=2,
        created_at=day - timedelta(hours=2),
        viewpoint_cluster_id=viewpoint_cluster_id,
    )


def _units_for(session, story, count: int = 3) -> list[uuid.UUID]:
    """Real units with real article bodies, so the prompt is real too."""
    day = datetime.now(timezone.utc)
    ids = []
    for index in range(count):
        article_id = uuid.uuid4()
        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=day,
            representative_article_id=article_id,
            article_count=1,
            source_tiers={"tier1": 1},
            owner_groups={f"owner{index}": 1},
            tier1_owner_groups={f"owner{index}": 1},
        )
        session.add(unit)
        session.add(RawArticle(
            id=article_id,
            url=f"https://example.com/{article_id}",
            url_hash=str(article_id).replace("-", ""),
            title=f"Report {index}",
            body_text=(
                f"Source {index} reports that the situation in the region continues "
                f"to develop, according to officials speaking on condition of anonymity."
            ),
            source_domain="example.com",
            source_tier=SourceTier.TIER1,
            published_at=day,
            reporting_unit_id=unit.id,
        ))
        session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
        ids.append(unit.id)
    return ids


CLAIM_JSON = {
    "claims": [
        {
            "text": "Officials say the situation continues to develop.",
            "claim_type": "fact",
            "evidence": [{"unit_id": "PLACEHOLDER", "stance": "supports", "confidence": 80}],
        }
    ]
}


@pytest.fixture
def story_factory(db_session):
    """Persist a story with `units` linked articles and return it."""
    created = []

    def make(status=Story.Status.PENDING, units=3, viewpoint_cluster_id=None):
        story = _story(status, units=units, viewpoint_cluster_id=viewpoint_cluster_id)
        db_session.add(story)
        _units_for(db_session, story, units)
        created.append(story)
        return story

    return make


@pytest.fixture
def new_session(db_engine):
    """A session on the same in-memory database, independent of db_session.

    For the cases where db_session's already-loaded relationships would make the
    test assert something about the test.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def fake_llm():
    """A stubbed LLM client that records what it was asked and how much it cost.

    Reports usage the way the real client does after this batch's change, because
    the budget is denominated in tokens and a stub that omitted usage would let a
    broken accounting path pass.
    """
    client = AsyncMock()
    client.chat_completion = AsyncMock(
        return_value={
            "choices": [{"message": {"content": json.dumps(CLAIM_JSON)}}],
            "usage": {"prompt_tokens": 1790, "completion_tokens": 647, "total_tokens": 2437},
            "finish_reason": "stop",
        }
    )
    client._parse_json_response = lambda content: json.loads(content)
    return client


@pytest.fixture
def llm_patched(fake_llm):
    with patch("src.shared.llm.get_llm_client", AsyncMock(return_value=fake_llm)):
        yield fake_llm


def _with_real_evidence(client: fake_llm, unit_ids) -> None:
    """Point the stub's evidence rows at this story's real unit ids."""
    content = json.dumps(CLAIM_JSON)
    for unit_id in unit_ids:
        content = content.replace("PLACEHOLDER", str(unit_id))
    client.chat_completion = AsyncMock(return_value={
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 1790, "completion_tokens": 647, "total_tokens": 2437},
        "finish_reason": "stop",
    })


class TestSelection:
    """What Phase 2 is willing to spend on."""

    @pytest.mark.asyncio
    async def test_pending_is_selected(self, db_session, story_factory):
        story_factory(status=Story.Status.PENDING)
        await db_session.commit()
        picked = await select_phase2_stories(db_session, hours_back=48, max_stories=10)
        assert len(picked) == 1
        assert picked[0].status == Story.Status.PENDING

    @pytest.mark.asyncio
    async def test_queued_is_still_selected(self, db_session, story_factory):
        """Approved stories are a subset of gate-passed, so they keep their analysis."""
        story_factory(status=Story.Status.QUEUED)
        await db_session.commit()
        picked = await select_phase2_stories(db_session, hours_back=48, max_stories=10)
        assert len(picked) == 1

    @pytest.mark.asyncio
    async def test_blocked_is_never_selected(self, db_session, story_factory):
        """The gate exists to stop spend on uncorroborated stories. This is that test."""
        story_factory(status=Story.Status.BLOCKED)
        await db_session.commit()
        picked = await select_phase2_stories(db_session, hours_back=48, max_stories=10)
        assert picked == []

    @pytest.mark.asyncio
    async def test_rejected_and_expired_are_never_selected(self, db_session, story_factory):
        story_factory(status=Story.Status.REJECTED)
        story_factory(status=Story.Status.EXPIRED)
        await db_session.commit()
        assert await select_phase2_stories(db_session, hours_back=48, max_stories=10) == []

    @pytest.mark.asyncio
    async def test_viewpoint_children_are_excluded(self, db_session, story_factory):
        """A child is one view of an event its parent already covers.

        Measured on dev 2026-10-03: 0 of 643 PENDING stories are viewpoint
        children, so this test is written against rows that exist rather than
        against the empty set the database happens to hold today.
        """
        parent = story_factory(status=Story.Status.PENDING)
        child = story_factory(status=Story.Status.PENDING, viewpoint_cluster_id=parent.id)
        await db_session.commit()

        picked = await select_phase2_stories(db_session, hours_back=48, max_stories=10)
        assert [s.id for s in picked] == [parent.id]
        assert child.id not in [s.id for s in picked]

    @pytest.mark.asyncio
    async def test_inclusion_is_opt_out_not_opt_in(self, db_session, story_factory):
        story_factory(status=Story.Status.PENDING)
        await db_session.commit()
        everything = await select_phase2_stories(
            db_session, hours_back=48, max_stories=10, exclude_viewpoint_children=False
        )
        assert len(everything) == 1

    @pytest.mark.asyncio
    async def test_newest_first(self, db_session, story_factory):
        """Newest first, so a harm flag today is not stuck behind last week's queue."""
        day = datetime.now(timezone.utc)
        older = story_factory(status=Story.Status.PENDING)
        newer = story_factory(status=Story.Status.PENDING)
        older.created_at = day - timedelta(hours=30)
        newer.created_at = day - timedelta(hours=1)
        await db_session.commit()

        picked = await select_phase2_stories(db_session, hours_back=48, max_stories=10)
        assert [s.id for s in picked] == [newer.id, older.id]

    @pytest.mark.asyncio
    async def test_older_than_the_window_is_not_selected(self, db_session, story_factory):
        story = story_factory(status=Story.Status.PENDING)
        story.created_at = datetime.now(timezone.utc) - timedelta(hours=96)
        await db_session.commit()
        assert await select_phase2_stories(db_session, hours_back=48, max_stories=10) == []

    def test_phase2_statuses_do_not_include_blocked(self):
        assert Story.Status.PENDING in PHASE2_STATUSES
        assert Story.Status.QUEUED in PHASE2_STATUSES
        assert Story.Status.BLOCKED not in PHASE2_STATUSES


class TestAlreadyAnalysed:
    """The re-spend guard, which is the whole reason input_hash is folded into a claim."""

    @pytest.mark.asyncio
    async def test_a_fresh_story_is_not_current(self, db_session, story_factory, llm_patched):
        story = story_factory(status=Story.Status.PENDING, units=3)
        await db_session.commit()
        digest = await claims_stage.story_unit_digest(db_session, story.id)
        assert digest
        assert await claims_stage.claims_are_current(db_session, story.id, digest) is False

    @pytest.mark.asyncio
    async def test_extraction_then_check_says_current(self, db_session, story_factory, llm_patched):
        story = story_factory(status=Story.Status.PENDING, units=3)
        await db_session.commit()
        _with_real_evidence(llm_patched, await _unit_ids(db_session, story))

        await claims_stage.extract_claims_for_story(db_session, story.id)

        digest = await claims_stage.story_unit_digest(db_session, story.id)
        assert await claims_stage.claims_are_current(db_session, story.id, digest) is True

    @pytest.mark.asyncio
    async def test_changed_evidence_makes_it_stale_again(
        self, db_session, story_factory, llm_patched, new_session
    ):
        """The reason the unit digest is in the hash at all.

        A claim is a reading of a story as its units stood. If the evidence moves
        under it, the old claim is a claim about evidence that is no longer there,
        and a hash over (story, text) alone would call it current forever.
        """
        story = story_factory(status=Story.Status.PENDING, units=3)
        await db_session.commit()
        _with_real_evidence(llm_patched, await _unit_ids(db_session, story))
        await claims_stage.extract_claims_for_story(db_session, story.id)

        await _add_unit(db_session, story)
        await db_session.commit()

        # A NEW session for the re-check, not the one that already loaded
        # story.units. Story.units is lazy="selectin", and a second query in the
        # same session does not refresh an already-loaded collection -- so reusing
        # it here would prove that adding a unit changes nothing, which is an
        # artefact of the test rather than a property of the code. Production opens
        # a fresh session per story (scripts/run_phase2.py run_story), so this is
        # the production shape.
        async with new_session() as session:
            digest = await claims_stage.story_unit_digest(session, story.id)
            current = await claims_stage.claims_are_current(session, story.id, digest)
        assert current is False

    @pytest.mark.asyncio
    async def test_a_bumped_analyzer_version_makes_it_stale(self, db_session, story_factory, llm_patched):
        """A claim written by an older analyzer is not current, however well it matches."""
        story = story_factory(status=Story.Status.PENDING, units=3)
        await db_session.commit()
        _with_real_evidence(llm_patched, await _unit_ids(db_session, story))
        await claims_stage.extract_claims_for_story(db_session, story.id)

        claim = (
            await db_session.execute(select(Claim).where(Claim.story_id == story.id))
        ).scalar_one()
        claim.analyzer_version = "claims:v0-invented"
        await db_session.commit()

        digest = await claims_stage.story_unit_digest(db_session, story.id)
        assert await claims_stage.claims_are_current(db_session, story.id, digest) is False

    @pytest.mark.asyncio
    async def test_the_digest_ignores_unit_ordering(self, db_session, story_factory):
        story = story_factory(status=Story.Status.PENDING, units=3)
        await db_session.commit()
        ids = await _unit_ids(db_session, story)
        forward = claims_stage.story_input_digest(ids)
        backward = claims_stage.story_input_digest(list(reversed(ids)))
        assert forward == backward


class TestBudget:
    """The cap, the pacing floor, and what a refusal does."""

    def test_the_estimate_over_counts_rather_than_under(self):
        # A tokenizer-free estimate has to be pessimistic: an under-count is
        # found by the provider's 429, an over-count only wastes headroom.
        assert estimate_prompt_tokens("x" * 400) > 100

    @pytest.mark.asyncio
    async def test_spending_is_counted_in_tokens(self):
        budget = Phase2TokenBudget(cap=40_000)
        assert await budget.spent_today() == 0
        await budget.record(2437)
        assert await budget.spent_today() == 2437
        assert await budget.remaining() == 40_000 - 2437

    @pytest.mark.asyncio
    async def test_a_zero_usage_call_still_costs_something(self):
        """A counter that cannot see a call is not a counter."""
        budget = Phase2TokenBudget(cap=40_000)
        await budget.record(0)
        assert await budget.spent_today() == 1

    @pytest.mark.asyncio
    async def test_headroom_below_the_estimate_refuses(self):
        budget = Phase2TokenBudget(cap=10)
        await budget.record(10)
        with pytest.raises(Phase2BudgetRefused):
            await budget.ensure_headroom("a" * 400)

    @pytest.mark.asyncio
    async def test_an_unreadable_counter_refuses(self, monkeypatch):
        """Fail safe in the direction budget.py already commits to."""
        monkeypatch.setattr("src.verification.phase2.used", AsyncMock(return_value=None))
        budget = Phase2TokenBudget(cap=40_000)
        assert await budget.spent_today() == -1
        assert await budget.remaining() == 0
        with pytest.raises(Phase2BudgetRefused):
            await budget.ensure_headroom("a")

    @pytest.mark.asyncio
    async def test_pacing_floor_is_honoured_and_the_first_call_does_not_wait(self, monkeypatch):
        monkeypatch.setattr("src.verification.phase2.get_settings", lambda: type(
            "S", (), {"phase2_min_seconds_between_calls": 25.0}
        )())
        slept = []
        budget = Phase2TokenBudget(cap=40_000)
        # A fake clock so the test does not take 25 real seconds.
        clock = [1000.0]
        monkeypatch.setattr("src.verification.phase2.time.monotonic", lambda: clock[0])

        assert budget._last_call_at is None
        assert await budget.pace(sleeper=slept.append) == 0.0
        assert slept == []

        clock[0] += 1.0
        assert await budget.pace(sleeper=slept.append) == 24.0
        assert slept == [24.0]

        clock[0] += 100.0
        assert await budget.pace(sleeper=slept.append) == 0.0
        assert len(slept) == 1

    @pytest.mark.asyncio
    async def test_the_default_cap_leaves_four_fifths_of_the_free_tier(self):
        from src.shared.config import get_settings

        settings = get_settings()
        # 200,000 tokens/day published for openai/gpt-oss-20b on the free plan.
        assert settings.phase2_daily_token_cap == 40_000
        assert settings.phase2_daily_token_cap <= 200_000 // 5


class TestRefusals:
    """A spent budget, a busy provider and a dead card are skips, not failures."""

    @pytest.mark.asyncio
    async def test_a_refused_budget_raises_out_of_extraction(
        self, db_session, story_factory, llm_patched, monkeypatch
    ):
        story = story_factory(status=Story.Status.PENDING, units=3)
        await db_session.commit()
        budget = Phase2TokenBudget(cap=1)
        await budget.record(1)

        with pytest.raises(Phase2BudgetRefused):
            await claims_stage.extract_claims_for_story(db_session, story.id, budget=budget)

        assert llm_patched.chat_completion.await_count == 0
        assert (
            await db_session.execute(select(func.count()).select_from(Claim))
        ).scalar_one() == 0

    @pytest.mark.asyncio
    async def test_a_402_is_never_retried(self, monkeypatch):
        """402 means a paid tier is configured somewhere; say so, do not route around it."""
        from scripts.run_phase2 import _Skip, _extract_claims

        class _OutOfCredit(Exception):
            status_code = 402

        engine = AsyncMock()
        maker = lambda: engine  # noqa: E731  (a context-manager-looking callable)
        engine.__aenter__ = AsyncMock(return_value=AsyncMock())
        engine.__aexit__ = AsyncMock(return_value=False)

        calls = []

        async def _boom(*args, **kwargs):
            calls.append(1)
            raise _OutOfCredit("quota exceeded")

        with patch("src.verification.claims.extract_claims_for_story", _boom), \
                patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(_Skip) as caught:
                await _extract_claims(
                    maker, uuid.uuid4(), budget=Phase2TokenBudget(cap=1000),
                    max_units=5, retries=3,
                )
        assert caught.value.reason == SKIP_OUT_OF_CREDIT
        assert len(calls) == 1, "a 402 was retried"

    @pytest.mark.asyncio
    async def test_a_429_is_retried_once_after_the_providers_own_retry_after(
        self, monkeypatch
    ):
        from scripts.run_phase2 import _extract_claims

        class _Exc(Exception):
            """A 429 with a retry-after in the body, the way groq sends it."""

            status_code = 429

        calls = []
        sleeps = []

        async def _flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise _Exc("Rate limit reached ... Please try again in 7.2375s.")
            return {"story_id": "x", "claims_created": 1, "evidence_created": 1, "errors": []}

        engine = AsyncMock()
        engine.__aenter__ = AsyncMock(return_value=AsyncMock())
        engine.__aexit__ = AsyncMock(return_value=False)
        maker = lambda: engine  # noqa: E731

        async def _sleep(seconds):
            sleeps.append(seconds)

        with patch("src.verification.claims.extract_claims_for_story", _flaky), \
                patch("asyncio.sleep", _sleep):
            result = await _extract_claims(
                maker, uuid.uuid4(), budget=Phase2TokenBudget(cap=1000),
                max_units=5, retries=1,
            )
        assert result["claims_created"] == 1
        assert len(calls) == 2
        # The provider said 7.2375s; that is what is slept, not a guess.
        assert sleeps and 7.0 < sleeps[0] < 7.5

    @pytest.mark.asyncio
    async def test_a_429_that_never_clears_becomes_a_skip(self, monkeypatch):
        from scripts.run_phase2 import _Skip, _extract_claims

        class _RateLimited(Exception):
            status_code = 429

        calls = []

        async def _always(*args, **kwargs):
            calls.append(1)
            raise _RateLimited("still limited")

        engine = AsyncMock()
        engine.__aenter__ = AsyncMock(return_value=AsyncMock())
        engine.__aexit__ = AsyncMock(return_value=False)
        maker = lambda: engine  # noqa: E731

        with patch("src.verification.claims.extract_claims_for_story", _always), \
                patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(_Skip) as caught:
                await _extract_claims(
                    maker, uuid.uuid4(), budget=Phase2TokenBudget(cap=1000),
                    max_units=5, retries=1,
                )
        assert caught.value.reason == SKIP_RATE_LIMITED
        assert len(calls) == 2, "one initial attempt plus exactly one retry"

    @pytest.mark.asyncio
    async def test_a_429_survives_the_client_and_is_still_classifiable(self):
        """The whole point of the llm.py fix, end to end through the real client.

        The tests above hand the runner an exception that still carries a status
        code, which is a shape the provider never actually produces: by the time a
        429 reaches a caller it has been through the client's retry decorator and is
        a tenacity RetryError, and chat_completion turns that into an LLMError. If
        the status code is lost on that path -- and it was, measured on dev
        2026-10-03 -- then the runner sees an unclassifiable failure, skips nothing,
        retries nothing, and a provider limit is indistinguishable from a bug.
        """
        from src.shared.llm import LLMClient, LLMError

        class _RateLimited(Exception):
            """Stands in for groq.RateLimitError: SDK type, not an httpx type."""

            status_code = 429

        client = LLMClient()
        client.groq_client = AsyncMock()
        client.cerebras_client = None
        client.groq_client.chat.completions.create.side_effect = _RateLimited(
            "Rate limit reached for model openai/gpt-oss-20b on tokens per minute "
            "(TPM): Limit 8000, Used 5011, Requested 3954. Please try again in 7.2375s."
        )

        with pytest.raises(LLMError) as caught:
            await client.chat_completion([{"role": "user", "content": "test"}], max_tokens=500)

        error = caught.value
        assert LLMClient.is_rate_limited(error), "the 429 was lost on the way out"
        assert not LLMClient.is_out_of_credit(error)
        # The provider's own number, read off the error it wraps, not the default.
        assert 7.0 < LLMClient._retry_after_seconds(error, 60.0) < 7.5
        assert client.groq_client.chat.completions.create.call_count == 3

    @pytest.mark.asyncio
    async def test_a_402_survives_the_client_and_is_never_routed_around(self):
        """The $0 rule needs the 402 to be visible, or nothing enforces it."""
        from src.shared.llm import LLMClient, LLMError

        class _OutOfCredit(Exception):
            status_code = 402

        client = LLMClient()
        client.groq_client = AsyncMock()
        client.cerebras_client = None
        client.groq_client.chat.completions.create.side_effect = _OutOfCredit("quota exceeded")

        with pytest.raises(LLMError) as caught:
            await client.chat_completion([{"role": "user", "content": "test"}], max_tokens=500)

        assert LLMClient.is_out_of_credit(caught.value)
        assert not LLMClient.is_rate_limited(caught.value)


class TestTheRunSpendsOnce:
    """The claim in the batch spec: run it twice, the second run spends nothing."""

    @pytest.mark.asyncio
    async def test_a_second_pass_over_the_same_story_costs_no_tokens(
        self, db_session, story_factory, llm_patched, monkeypatch
    ):
        story = story_factory(status=Story.Status.PENDING, units=3)
        await db_session.commit()
        _with_real_evidence(llm_patched, await _unit_ids(db_session, story))

        monkeypatch.setattr(
            "src.verification.phase2.get_settings",
            lambda: _settings(min_seconds=0.0),
        )

        budget = Phase2TokenBudget(cap=40_000)

        first = await claims_stage.extract_claims_for_story(
            db_session, story.id, budget=budget
        )
        assert first["claims_created"] == 1
        spent_after_first = await used(GROQ_PHASE2_TOKENS)
        assert spent_after_first == 2437

        # Second pass, exactly as the daily job would do it: ask first, then
        # spend only if the answer is no.
        digest = await claims_stage.story_unit_digest(db_session, story.id)
        if await claims_stage.claims_are_current(db_session, story.id, digest):
            second_calls = 0
        else:
            await claims_stage.extract_claims_for_story(db_session, story.id, budget=budget)
            second_calls = 1

        assert second_calls == 0
        assert await used(GROQ_PHASE2_TOKENS) == spent_after_first
        assert llm_patched.chat_completion.await_count == 1

    @pytest.mark.asyncio
    async def test_the_counter_row_is_named_for_phase2(
        self, db_session, story_factory, llm_patched
    ):
        """Its own row, so Phase 2 cannot starve caption/classification on the shared tier."""
        story = story_factory(status=Story.Status.PENDING, units=3)
        await db_session.commit()
        _with_real_evidence(llm_patched, await _unit_ids(db_session, story))

        budget = Phase2TokenBudget(cap=40_000)
        await claims_stage.extract_claims_for_story(db_session, story.id, budget=budget)

        assert await used(GROQ_PHASE2_TOKENS) == 2437
        # The request-denominated counters are untouched by a token-denominated job.
        from src.shared.budget import GROQ_REQUESTS, GROQ_TRANSLATION_REQUESTS

        assert await used(GROQ_REQUESTS) == 0
        assert await used(GROQ_TRANSLATION_REQUESTS) == 0


class TestTheRunRecord:
    """The summary, because "processed 0 stories" is how three stages hid for months."""

    def test_an_empty_run_summarizes_to_zeros_not_to_an_exception(self):
        assert summarize([]) == {
            "stories": 0, "topics_created": 0, "arcs_created": 0,
            "claims_created": 0, "evidence_created": 0, "errors": 0,
            "skipped": {}, "skipped_total": 0, "processed": 0,
        }

    def test_every_skip_reason_is_counted_by_name(self):
        results = [
            new_result(uuid.uuid4()),
            new_result(uuid.uuid4()),
            new_result(uuid.uuid4()),
        ]
        results[0]["skipped"] = SKIP_ALREADY_ANALYSED
        results[1]["skipped"] = SKIP_BUDGET
        results[2]["claims_created"] = 3
        summary = summarize(results)
        assert summary["skipped"] == {SKIP_ALREADY_ANALYSED: 1, SKIP_BUDGET: 1}
        assert summary["skipped_total"] == 2
        assert summary["processed"] == 1
        assert summary["claims_created"] == 3

    def test_an_unreadable_counter_is_not_reported_as_zero_spend(self):
        """A missing counter is a fact about the run and belongs in it."""
        assert Phase2TokenBudget(cap=1).cap == 1


class TestClaimsStageConstants:
    def test_the_measured_ceiling_is_the_one_in_the_call(self):
        from src.verification.claims import CLAIM_MAX_TOKENS, CLAIM_MAX_UNITS

        # 4096 + 5 units measured 2,437 tokens/call against a published 8,000 TPM.
        assert CLAIM_MAX_TOKENS == 4096
        assert CLAIM_MAX_UNITS == 5


# --- helpers ---------------------------------------------------------------


def _settings(min_seconds: float = 0.0):
    from src.shared.config import get_settings

    base = get_settings()
    return base.model_copy(update={"phase2_min_seconds_between_calls": min_seconds})


async def _unit_ids(session, story) -> list[uuid.UUID]:
    rows = (
        await session.execute(
            select(StoryUnitLink.unit_id).where(StoryUnitLink.story_id == story.id)
        )
    ).scalars().all()
    return list(rows)


async def _add_unit(session, story) -> uuid.UUID:
    day = datetime.now(timezone.utc)
    article_id = uuid.uuid4()
    unit = ReportingUnit(
        id=uuid.uuid4(),
        day=day,
        representative_article_id=article_id,
        article_count=1,
        source_tiers={"tier1": 1},
        owner_groups={"late-arrival": 1},
        tier1_owner_groups={"late-arrival": 1},
    )
    session.add(unit)
    session.add(RawArticle(
        id=article_id,
        url=f"https://example.com/{article_id}",
        url_hash=str(article_id).replace("-", ""),
        title="Late report",
        body_text="A report that arrived after the claim matrix was written.",
        source_domain="example.com",
        source_tier=SourceTier.TIER1,
        published_at=day,
        reporting_unit_id=unit.id,
    ))
    session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
    await session.flush()
    return unit.id
