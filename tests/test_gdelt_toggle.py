"""Tests for the GDELT enable/disable toggle, via settings.gdelt_enabled.

The switch is real and it is one line: ``build_adapters`` appends
``GDELTAdapter()`` only ``if settings.gdelt_enabled and SourceTier.TIER1 in
tiers`` (src/ingestion/run.py:263). These tests drive that line and assert on
what it produced.

They previously asserted on ``src.ingestion.gdelt.ingest_gdelt``, a function
``run_ingestion`` has never called since the adapters landed -- it calls
``adapter.fetch()``. So ``assert_called_once()`` was asserting that a dead name
was called exactly once, and ``assert_not_called()`` was asserting it was
called zero times: both passed no matter what the toggle did, and
``test_gdelt_enabled_called_once`` was red for a reason that had nothing to do
with the toggle.
"""

import pytest

from src.ingestion import run as run_module
from tests.ingestion_harness import make_article


def _settings_with(gdelt_enabled: bool):
    """Settings whose ``gdelt_enabled`` is a genuine bool.

    A ``MagicMock`` settings object makes ``settings.gdelt_enabled`` truthy for
    any assigned value, including one that looks False-ish, so the old tests
    could not have detected an inverted toggle.
    """
    from tests.ingestion_harness import settings_for

    return settings_for(gdelt_enabled=gdelt_enabled)


class TestGdeltToggle:
    """settings.gdelt_enabled controls whether the GDELT adapter runs."""

    @pytest.mark.asyncio
    async def test_gdelt_disabled_never_called(self, monkeypatch, ingestion_env):
        """With gdelt_enabled=False, no GDELT adapter is built or fetched."""
        monkeypatch.setattr(
            run_module, "get_settings", lambda: _settings_with(False)
        )
        run = ingestion_env.fetch({"rss_tier1": []})

        results = await ingestion_env.ingest(dry_run=True)

        # The mechanism: the real build_adapters did not select a GDELT
        # adapter, so there was nothing to run. Asserted on the constructed
        # list, not on a mock of a function nobody calls.
        assert "gdelt" not in run.constructed
        assert "gdelt" not in run.fetched
        assert run.constructed, "no adapters were built at all, so this proves nothing"

        # And nothing in the reported results claims GDELT ran.
        ingestion = results["phases"]["ingestion"]
        assert ingestion["gdelt"] == 0
        assert "gdelt" not in ingestion["adapter_health"]

    @pytest.mark.asyncio
    async def test_gdelt_enabled_called_once(self, monkeypatch, ingestion_env):
        """With gdelt_enabled=True, exactly one GDELT adapter is built and fetched."""
        monkeypatch.setattr(
            run_module, "get_settings", lambda: _settings_with(True)
        )
        run = ingestion_env.fetch(
            {
                "rss_tier1": [],
                "gdelt": [
                    make_article(
                        "https://bbc.com/article/toggle-probe",
                        "Body proving the GDELT adapter's articles were ingested.",
                        "bbc.com",
                    )
                ],
            }
        )

        # A real (non-dry) run, so the GDELT-sourced article is an actual row
        # and not just a number in a results dict.
        results = await ingestion_env.ingest(dry_run=False)

        ingestion = results["phases"]["ingestion"]

        # The mechanism, asserted three ways so no single loose end passes:
        # selected once, fetched once, and its articles actually counted.
        assert run.constructed.count("gdelt") == 1
        assert run.fetched.count("gdelt") == 1
        assert ingestion["gdelt"] == 1
        assert ingestion["total_fetched"] == 1
        assert ingestion["total_new"] == 1

        # The artifact: the GDELT-sourced article is a real row, and the
        # reported health comes from the GDELT adapter rather than the
        # "GDELT adapter not run" fallback used when the toggle is off.
        rows = await ingestion_env.rows()
        assert [r[0] for r in rows] == ["https://bbc.com/article/toggle-probe"]
        assert ingestion["adapter_health"]["gdelt"]["status"] == "ok"
        assert ingestion["adapter_health"]["gdelt"]["succeeded"] == ["gdelt"]

    @pytest.mark.asyncio
    async def test_gdelt_off_yields_the_adapter_not_run_fallback(
        self, monkeypatch, ingestion_env
    ):
        """Disabled, the health block is the documented "not run" fallback.

        run.py reads ``adapter_health.get("gdelt", ...)`` and falls back to a
        "down / GDELT adapter not run" shape. With the toggle off that fallback
        is what a reader sees, and it is the state the pipeline is designed to
        degrade to, so it is worth pinning: the "degraded" reporting downstream
        depends on distinguishing "switched off" from "tried and failed".
        """
        monkeypatch.setattr(
            run_module, "get_settings", lambda: _settings_with(False)
        )
        ingestion_env.fetch({"rss_tier1": []})

        results = await ingestion_env.ingest(dry_run=True)

        gdelt_health = results["phases"]["ingestion"]["gdelt_health"]
        assert gdelt_health == {"succeeded": [], "failed": [], "skipped": []}
        # No adapter_health entry at all, because no GDELT adapter existed.
        assert "gdelt" not in results["phases"]["ingestion"]["adapter_health"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
