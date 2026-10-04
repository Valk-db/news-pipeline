"""Tests for the LLM roster: the one ordered list of rungs, and its staleness guard.

Two things are being pinned here, and they are not the same kind of thing.

The first is that a hard-coded `:free` model id rots. `openai/gpt-oss-20b:free` -- the
backup this roster was originally asked to wire up -- is absent from OpenRouter's entire
466-model catalogue, not merely from its free subset. A test that only checks the roster
is well-formed would keep passing while every request to that rung 404s, so
`stale_free_ids` is tested against both a retirement and a rename.

The second is that a rung with no credential never appears in the walk. That is not
bookkeeping: `has_llm` decides whether a run may claim it can curate at all, and a rung
that reported itself live on a magic-attribute lookup would make a deployment with no
keys look configured.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.shared.budget import (
    CEREBRAS_REQUESTS,
    GROQ_REQUESTS,
    OPENROUTER_GEMMA_REQUESTS,
    OPENROUTER_NEMOTRON_REQUESTS,
)
from src.shared.llm_roster import (
    ROSTER,
    RosterCheck,
    check_free_model_roster,
    configured_free_ids,
    configured_rungs,
    live_rungs,
    model_for,
    stale_free_ids,
)


def _settings(**overrides):
    base = dict(
        groq_api_key="gsk_test",
        groq_model="",
        cerebras_api_key="",
        cerebras_model="",
        openrouter_api_key="",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_timeout=30.0,
        openrouter_gemma_model="",
        openrouter_nemotron_model="",
    )
    base.update(overrides)
    return MagicMock(**base)


class TestRosterShape:
    def test_rung_names_are_unique(self):
        names = [r.name for r in ROSTER]
        assert len(names) == len(set(names))

    def test_budget_rows_are_unique_per_rung(self):
        """The point of per-rung rows: a shared row would let one model's outage report
        itself as another model's exhausted budget, which is exactly what a shared
        upstream pool produces."""
        names = [r.budget_name for r in ROSTER]
        assert len(names) == len(set(names))

    def test_every_rung_names_real_settings(self):
        """Attribute names are non-empty and lowercase.

        This does NOT verify the names exist on Settings; see
        tests/test_roster_attr_consistency.py for the test that does.
        """
        for rung in ROSTER:
            for attr in (rung.key_attr, rung.model_attr, rung.daily_cap_attr, rung.minute_cap_attr):
                assert attr, f"{rung.name} has an unnamed settings field"
                assert attr.islower()

    def test_free_rungs_are_pinned_to_a_free_id(self):
        for rung in ROSTER:
            if rung.free_tier:
                assert rung.model.endswith(":free"), f"{rung.name} is not pinned to a free id"

    def test_retired_backup_id_is_gone(self):
        """The reason the backup rung is not openai/gpt-oss-20b:free."""
        assert not any("gpt-oss-20b" in r.model and r.free_tier for r in ROSTER)

    def test_groq_is_first(self):
        """A decision, not a law: Groq is the only rung whose credential this pipeline is
        known to hold in CI, so it stays first. Asserted so a later edit that reshuffles
        the order has to say so out loud."""
        assert ROSTER[0].name == "groq"

    def test_cerebras_is_last(self):
        assert ROSTER[-1].name == "cerebras"


class TestModelResolution:
    def test_pinned_id_is_used_when_the_setting_is_empty(self):
        settings = _settings()
        assert model_for(ROSTER[0], settings) == "openai/gpt-oss-20b"

    def test_setting_overrides_the_pinned_id(self):
        """A retired id must be swappable without a code change, which is the whole reason
        the setting exists."""
        settings = _settings(groq_model="llama-3.3-70b-versatile")
        assert model_for(ROSTER[0], settings) == "llama-3.3-70b-versatile"

    def test_whitespace_only_setting_falls_back_to_the_pin(self):
        assert model_for(ROSTER[0], _settings(groq_model="   ")) == "openai/gpt-oss-20b"

    def test_configured_free_ids_reflects_overrides(self):
        settings = _settings(openrouter_gemma_model="google/gemma-9-9b-it:free")
        ids = configured_free_ids(settings)
        assert "google/gemma-9-9b-it:free" in ids
        assert "google/gemma-4-26b-a4b-it:free" not in ids


class TestStaleness:
    def test_a_retired_id_is_reported_stale(self):
        listed = {"nvidia/nemotron-3-super-120b-a12b:free"}
        assert stale_free_ids(["google/gemma-4-26b-a4b-it:free"], listed) == [
            "google/gemma-4-26b-a4b-it:free"
        ]

    def test_a_rename_is_reported_stale(self):
        """A renamed free tier keeps working under the old id for nobody: the symptom is a
        404 on every request, which with a fallback chain reads as 'the pool is busy'."""
        listed = {"google/gemma-4-26b-a4b-it-v2:free"}
        assert stale_free_ids(["google/gemma-4-26b-a4b-it:free"], listed) == [
            "google/gemma-4-26b-a4b-it:free"
        ]

    def test_roster_order_is_preserved_in_the_report(self):
        configured = ["a:free", "b:free", "c:free"]
        assert stale_free_ids(configured, {"b:free"}) == ["a:free", "c:free"]

    def test_all_present_means_none_stale(self):
        assert stale_free_ids(["a:free"], {"a:free", "b:free"}) == []

    @pytest.mark.asyncio
    async def test_an_unreachable_catalogue_is_not_a_pass(self):
        """A check that could not run must not report success. Silently green here is how
        a retired id survives a year."""
        settings = _settings(openrouter_api_key="sk-or-test")
        with patch(
            "src.shared.llm_roster.fetch_free_model_ids",
            new=AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            check = await check_free_model_roster(settings)
        assert check.ok is False
        assert "catalogue unreachable" in check.error
        assert check.stale == ()

    @pytest.mark.asyncio
    async def test_stale_roster_fails_the_check(self):
        settings = _settings(openrouter_api_key="sk-or-test")
        with patch(
            "src.shared.llm_roster.fetch_free_model_ids",
            new=AsyncMock(return_value={"nvidia/nemotron-3-super-120b-a12b:free"}),
        ):
            check = await check_free_model_roster(settings)
        assert check.ok is False
        assert check.stale == ("google/gemma-4-26b-a4b-it:free",)
        assert "STALE" in check.render()

    @pytest.mark.asyncio
    async def test_live_roster_passes_and_renders_every_id(self):
        settings = _settings(openrouter_api_key="sk-or-test")
        every = {r.model for r in ROSTER if r.free_tier}
        with patch(
            "src.shared.llm_roster.fetch_free_model_ids",
            new=AsyncMock(return_value=every),
        ):
            check = await check_free_model_roster(settings)
        assert check.ok is True
        assert check.stale == ()
        assert check.listed == len(every)
        for model_id in every:
            assert model_id in check.render()

    @pytest.mark.asyncio
    async def test_no_key_means_nothing_to_check_not_a_failure(self):
        """An unkeyed deployment has no free ids in play, so a red mark would be a false
        alarm about a problem it does not have."""
        check = await check_free_model_roster(_settings(openrouter_api_key=""))
        assert check.ok is True
        assert check.listed == 0


class TestRosterFiltering:
    def test_configured_rungs_follows_credentials_not_clients(self):
        """Preflight answers 'is this key usable', so it must not read an injected object."""
        settings = _settings(groq_api_key="gsk_test", cerebras_api_key="")
        assert [r.name for r in configured_rungs(settings)] == ["groq"]

    def test_one_openrouter_key_enables_every_free_rung(self):
        settings = _settings(groq_api_key="", openrouter_api_key="sk-or-test")
        assert [r.name for r in configured_rungs(settings)] == [
            "openrouter:gemma",
            "openrouter:nemotron",
        ]

    def test_blank_key_is_not_a_credential(self):
        assert configured_rungs(_settings(groq_api_key="   ")) == []

    def test_live_rungs_reads_client_attributes(self):
        """The tests inject a mock with no key configured; that has to count as live, or
        every fallback test in the suite becomes unreachable."""

        class FakeClient:
            groq_client = AsyncMock()
            cerebras_client = None
            _openrouter_key = None

        assert [r.name for r in live_rungs(FakeClient())] == ["groq"]

    def test_live_rungs_covers_injected_openrouter(self):
        class FakeClient:
            groq_client = None
            cerebras_client = None
            _openrouter_key = "sk-or-test"

        assert [r.name for r in live_rungs(FakeClient())] == [
            "openrouter:gemma",
            "openrouter:nemotron",
        ]

    def test_live_rungs_preserves_roster_order(self):
        class FakeClient:
            groq_client = AsyncMock()
            cerebras_client = AsyncMock()
            _openrouter_key = "sk-or-test"

        assert [r.name for r in live_rungs(FakeClient())] == [r.name for r in ROSTER]

    def test_a_settings_double_is_not_a_credential(self):
        """MagicMock invents a truthy attribute for any name. Without the type check a
        preflight run on a mock reports four configured providers instead of none."""

        class FakeSettings:
            groq_api_key = MagicMock()
            cerebras_api_key = MagicMock()
            openrouter_api_key = MagicMock()

        assert configured_rungs(FakeSettings()) == []


class TestBudgetRows:
    def test_each_provider_owns_its_row(self):
        rows = {r.name: r.budget_name for r in ROSTER}
        assert rows["groq"] == GROQ_REQUESTS
        assert rows["cerebras"] == CEREBRAS_REQUESTS
        assert rows["openrouter:gemma"] == OPENROUTER_GEMMA_REQUESTS
        assert rows["openrouter:nemotron"] == OPENROUTER_NEMOTRON_REQUESTS


@pytest.mark.parametrize("check", [RosterCheck(True, ("a:free",), 3, ())])
def test_render_always_lists_configured_ids(check):
    assert "a:free" in check.render()
