"""Tests for tier classification and tier-1 gate logic."""

import uuid

import pytest

from src.verification.tiers import TIER1_DOMAINS, TIER2_DOMAINS, classify_source_tier, evaluate_tier1_gate
from src.verification.units import get_owner_group
from src.schema.models import SourceTier
from datetime import UTC


class TestClassifySourceTier:
    def test_tier1_domains(self):
        for domain in TIER1_DOMAINS:
            assert classify_source_tier(domain) == SourceTier.TIER1

    def test_tier2_domains(self):
        for domain in TIER2_DOMAINS:
            assert classify_source_tier(domain) == SourceTier.TIER2

    def test_unknown_domain_defaults_to_tier3(self):
        assert classify_source_tier("randomblog.com") == SourceTier.TIER3
        assert classify_source_tier("example.org") == SourceTier.TIER3

    def test_case_sensitive(self):
        assert classify_source_tier("BBC.COM") == SourceTier.TIER3
        assert classify_source_tier("ApNews.com") == SourceTier.TIER3


class TestTierConstants:
    def test_tier1_not_empty(self):
        assert len(TIER1_DOMAINS) > 0

    def test_tier2_not_empty(self):
        assert len(TIER2_DOMAINS) > 0

    def test_no_overlap(self):
        overlap = TIER1_DOMAINS & TIER2_DOMAINS
        assert overlap == set()

    def test_core_tier1_present(self):
        expected = {"bbc.com", "theguardian.com", "npr.org", "apnews.com", "reuters.com"}
        assert expected.issubset(TIER1_DOMAINS)


class TestEvaluateTier1Gate:
    # The gate consumes one (unit_id, owner) pair per distinct tier-1 owner of each unit
    # (Corroboration.tier1_pairs): the unit half of the rule counts distinct unit ids, so two
    # articles inside one reporting unit are one unit's worth of corroboration.

    def test_passes_with_two_tier1_different_owners(self):
        units = [
            (uuid.uuid4(), "AP"),
            (uuid.uuid4(), "Reuters"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True
        assert "Gate passed" in reason

    def test_passes_with_three_tier1_two_owners(self):
        units = [
            (uuid.uuid4(), "AP"),
            (uuid.uuid4(), "Reuters"),
            (uuid.uuid4(), "BBC"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True

    def test_fails_only_one_tier1_unit(self):
        units = [
            (uuid.uuid4(), "AP"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 1 tier-1 unit" in reason

    def test_fails_two_tier1_same_owner(self):
        units = [
            (uuid.uuid4(), "AP"),
            (uuid.uuid4(), "AP"),  # Same owner group, two units
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "owner" in reason.lower()

    def test_fails_no_tier1_units(self):
        units = []
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 0 tier-1 units" in reason

    def test_fails_empty_list(self):
        units = []
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 0 tier-1 units" in reason

    def test_one_unit_two_owners_is_still_one_unit(self):
        # P0-4: one reporting event republished by two tier-1 outlets is one unit, not two.
        # tier1_pairs() emits one pair per (unit, owner), so the gate sees two pairs but one
        # distinct unit id and must block.
        unit_id = uuid.uuid4()
        units = [
            (unit_id, "AP"),
            (unit_id, "Reuters"),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False
        assert "Only 1 tier-1 unit" in reason

    def test_mixed_tiers_and_owners(self):
        # Two units, both tier-1 from AP: the units half passes, the owners half fails.
        units = [
            (uuid.uuid4(), "AP"),
            (uuid.uuid4(), "AP"),   # Two tier-1 units from AP
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is False  # Only one distinct owner (AP) from tier-1
        assert "owner" in reason.lower()

    def test_get_owner_group_integration(self):
        # Verify the gate uses owner groups correctly
        units = [
            (uuid.uuid4(), get_owner_group("apnews.com")),
            (uuid.uuid4(), get_owner_group("reuters.com")),
        ]
        should_queue, reason = evaluate_tier1_gate(units)
        assert should_queue is True

        units_same = [
            (uuid.uuid4(), get_owner_group("apnews.com")),
            (uuid.uuid4(), get_owner_group("www.apnews.com")),  # Same owner group
        ]
        should_queue, reason = evaluate_tier1_gate(units_same)
        assert should_queue is False

class TestGateReasonCountsUnits:
    # P0 deferred (b), 2026-10-02: Story.tier1_unit_count is the raw tier-1 *article* volume
    # (summed per-unit article histograms), but the gate decides on distinct reporting units.
    # The "Passed gate:" sentence must print the unit count the gate counted, not the
    # article volume -- a unit carrying two tier-1 articles is one unit's worth of
    # corroboration, and the sentence must not claim two.

    async def _article(self, session, domain):
        from datetime import datetime
        from src.schema.models import RawArticle

        article = RawArticle(
            id=uuid.uuid4(),
            url=f"https://{domain}/u/{uuid.uuid4().hex[:8]}",
            url_hash=uuid.uuid4().hex,
            title=f"Report from {domain}",
            body_text="Body.",
            source_domain=domain,
            source_tier=SourceTier.TIER1,
            published_at=datetime.now(UTC),
        )
        session.add(article)
        await session.flush()
        return article

    async def _unit(self, session, articles):
        from datetime import datetime
        from src.schema.models import ReportingUnit

        source_tiers, owner_groups, tier1_owner_groups = {}, {}, {}
        for article in articles:
            owner = get_owner_group(article.source_domain)
            source_tiers["tier1"] = source_tiers.get("tier1", 0) + 1
            owner_groups[owner] = owner_groups.get(owner, 0) + 1
            tier1_owner_groups[owner] = tier1_owner_groups.get(owner, 0) + 1
        unit = ReportingUnit(
            id=uuid.uuid4(),
            day=datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0),
            representative_article_id=articles[0].id,
            article_count=len(articles),
            source_tiers=source_tiers,
            owner_groups=owner_groups,
            tier1_owner_groups=tier1_owner_groups,
        )
        session.add(unit)
        await session.flush()
        for article in articles:
            article.reporting_unit_id = unit.id
        return unit

    async def _story(self, session, units):
        from datetime import datetime
        from src.schema.models import Story, StoryUnitLink

        story = Story(
            id=uuid.uuid4(),
            day=datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0),
            primary_entities=[],
            status=Story.Status.PENDING,
        )
        session.add(story)
        await session.flush()
        for unit in units:
            session.add(StoryUnitLink(story_id=story.id, unit_id=unit.id))
        await session.flush()
        await session.commit()
        return story

    @pytest.mark.asyncio
    async def test_passed_sentence_reports_units_not_articles(self, db_session):
        from src.verification.tiers import apply_tier1_gate

        # One unit carrying TWO tier-1 articles (two owners), plus a second unit with one:
        # 2 units, 3 distinct owners, but 3 tier-1 articles.
        unit1 = await self._unit(
            db_session,
            [await self._article(db_session, d) for d in ("bbc.com", "theguardian.com")],
        )
        unit2 = await self._unit(db_session, [await self._article(db_session, "npr.org")])
        story = await self._story(db_session, [unit1, unit2])

        result = await apply_tier1_gate(db_session, [story.id])
        assert result == {"queued": 1, "blocked": 0}

        await db_session.refresh(story)
        # The stored counter is still the raw article volume...
        assert story.tier1_unit_count == 3
        # ...but the sentence reports the units the gate decided on.
        assert story.gate_reason == "Passed gate: 2 tier-1 units, 3 distinct owners"
