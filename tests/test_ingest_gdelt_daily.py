"""Tests for the daily worker shim: arg passing, exit code propagation, and
the dead man's switch.

The pipeline subprocess is mocked; nothing here runs the real pipeline.
"""

import importlib.util
import os
import sys

import pytest

from src.ingestion.run import parse_args, build_adapters, parse_tiers
from src.schema.models import SourceTier


# ------------------------------------------------------- worker module load

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "ingest_gdelt_daily.py",
)


def load_worker():
    """Import scripts/ingest_gdelt_daily.py by path; scripts/ is a package."""
    spec = importlib.util.spec_from_file_location("ingest_gdelt_daily", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load_worker()

# A minimal run.py results document where every required stage reported items,
# so argument forwarding tests are not coupled to the stage check.
HEALTHY_OUTPUT = (
    "log line\n"
    + '{\n  "phases": {\n    "ingestion": {\n      "total_fetched": 40,\n'
    '      "total_new": 12\n    },\n    "reporting_units": {\n'
    '      "created": 3\n    },\n    "gate": {\n      "queued": 5\n    }\n  }\n}\n'
)


# ----------------------------------------------------------- arg forwarding


class TestWorkerArgPassing:
    def test_passes_env_dev_to_subprocess(self, monkeypatch):
        seen = {}

        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            seen["cmd"] = cmd
            seen["cwd"] = cwd
            return type("P", (), {"returncode": 0, "stdout": HEALTHY_OUTPUT, "stderr": ""})()

        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        monkeypatch.setattr(worker, "send_dead_man_signal", lambda: None)
        monkeypatch.setattr(worker.sys, "argv", ["ingest_gdelt_daily.py"])

        assert worker.main() == 0

        assert "--env" in seen["cmd"]
        assert seen["cmd"][seen["cmd"].index("--env") + 1] == "dev"
        assert seen["cmd"][1:3] == ["-m", "src.ingestion.run"]
        assert seen["cwd"] == worker.REPO_ROOT

    def test_passes_sources_and_tiers_through(self, monkeypatch):
        seen = {}

        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            seen["cmd"] = cmd
            return type("P", (), {"returncode": 0, "stdout": HEALTHY_OUTPUT, "stderr": ""})()

        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        monkeypatch.setattr(worker, "send_dead_man_signal", lambda: None)
        monkeypatch.setattr(
            worker.sys, "argv",
            ["ingest_gdelt_daily.py", "--sources", "sensors,gdelt", "--tiers", "tier3"],
        )

        assert worker.main() == 0
        cmd = seen["cmd"]
        assert cmd[cmd.index("--sources") + 1] == "sensors,gdelt"
        assert cmd[cmd.index("--tiers") + 1] == "tier3"

    def test_env_override_is_respected(self, monkeypatch):
        seen = {}

        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            seen["cmd"] = cmd
            return type("P", (), {"returncode": 0, "stdout": HEALTHY_OUTPUT, "stderr": ""})()

        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        monkeypatch.setattr(worker, "send_dead_man_signal", lambda: None)
        monkeypatch.setattr(
            worker.sys, "argv", ["ingest_gdelt_daily.py", "--env", "prod"]
        )

        assert worker.main() == 0
        cmd = seen["cmd"]
        assert cmd[cmd.index("--env") + 1] == "prod"


# ----------------------------------------------------- exit code propagation


class TestExitCodePropagation:
    def test_nonzero_exit_propagates(self, monkeypatch):
        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            return type("P", (), {"returncode": 3, "stdout": "", "stderr": "boom"})()

        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        monkeypatch.setattr(worker.sys, "argv", ["ingest_gdelt_daily.py"])

        assert worker.main() == 3

    def test_failed_run_does_not_ping_healthcheck(self, monkeypatch):
        pinged = []

        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        monkeypatch.setattr(worker, "send_dead_man_signal", lambda: pinged.append(1))
        monkeypatch.setattr(worker.sys, "argv", ["ingest_gdelt_daily.py"])

        assert worker.main() == 1
        assert pinged == []

    def test_zero_item_stage_exits_nonzero(self, monkeypatch):
        output = (
            "Phase 1: Ingesting articles...\n"
            + '{\n  "phases": {\n    "ingestion": {\n      "total_fetched": 0,\n'
            '      "total_new": 0\n    },\n    "reporting_units": {\n'
            '      "created": 3\n    },\n    "gate": {\n      "queued": 5\n    }\n  }\n}\n'
        )

        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            return type("P", (), {"returncode": 0, "stdout": output, "stderr": ""})()

        pinged = []
        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        monkeypatch.setattr(worker, "send_dead_man_signal", lambda: pinged.append(1))
        monkeypatch.setattr(worker.sys, "argv", ["ingest_gdelt_daily.py"])

        assert worker.main() == 1
        assert pinged == []

    def test_missing_results_object_fails_loudly(self, monkeypatch):
        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            return type("P", (), {"returncode": 0, "stdout": "no json here", "stderr": ""})()

        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        monkeypatch.setattr(worker, "send_dead_man_signal", lambda: None)
        monkeypatch.setattr(worker.sys, "argv", ["ingest_gdelt_daily.py"])

        assert worker.main() == 1

    def test_healthy_run_exits_zero_and_pings(self, monkeypatch):
        output = (
            "log line\n"
            + '{\n  "phases": {\n    "ingestion": {\n      "total_fetched": 40,\n'
            '      "total_new": 12\n    },\n    "reporting_units": {\n'
            '      "created": 3\n    },\n    "gate": {\n      "queued": 5\n    }\n  }\n}\n'
        )

        def fake_run(cmd, cwd=None, capture_output=None, text=None):
            return type("P", (), {"returncode": 0, "stdout": output, "stderr": ""})()

        pinged = []
        monkeypatch.setattr(worker.subprocess, "run", fake_run)
        monkeypatch.setattr(worker, "send_dead_man_signal", lambda: pinged.append(1))
        monkeypatch.setattr(worker.sys, "argv", ["ingest_gdelt_daily.py"])

        assert worker.main() == 0
        assert pinged == [1]


# -------------------------------------------------------- stage extraction


class TestStageExtraction:
    def test_extracts_results_json_from_noise(self):
        output = 'log\nlog\n{\n  "phases": {\n    "ingestion": {\n      "total_fetched": 7\n    }\n  }\n}\ntrailing\n'
        results = worker.extract_results(output)
        assert results["phases"]["ingestion"]["total_fetched"] == 7

    def test_handles_braces_inside_strings(self):
        output = '{"phases": {"ingestion": {"detail": "a } brace", "total_fetched": 2}}}'
        results = worker.extract_results(output)
        assert results["phases"]["ingestion"]["total_fetched"] == 2

    def test_no_json_returns_empty(self):
        assert worker.extract_results("nothing") == {}

    def test_stage_counts_flatten_phases(self):
        results = {"phases": {"ingestion": {"total_fetched": 5}, "gate": {"queued": 1}}}
        counts = worker.stage_counts(results)
        assert counts["ingestion.total_fetched"] == 5
        assert counts["gate.queued"] == 1

    def test_check_stages_names_the_empty_stage(self):
        output = '{"phases": {"ingestion": {"total_fetched": 0, "total_new": 0}, "reporting_units": {"created": 1}, "gate": {"queued": 0}}}'
        empty = worker.check_stages(output)
        assert set(empty) == {
            "ingestion.total_fetched",
            "ingestion.total_new",
            "gate.queued",
        }

    def test_check_stages_passes_when_all_present(self):
        output = '{"phases": {"ingestion": {"total_fetched": 1, "total_new": 1}, "reporting_units": {"created": 1}, "gate": {"queued": 1}}}'
        assert worker.check_stages(output) == []


# ------------------------------------------------------ dead man's switch


class TestHealthcheck:
    def test_ping_uses_uuid_in_url(self, monkeypatch):
        seen = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(url, timeout=None):
            seen["url"] = url
            return FakeResponse()

        monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
        assert worker.ping_healthcheck("abc-123") is True
        assert "abc-123" in seen["url"]

    def test_ping_failure_returns_false(self, monkeypatch):
        import urllib.error

        def fake_urlopen(url, timeout=None):
            raise urllib.error.URLError("unreachable")

        monkeypatch.setattr(worker.urllib.request, "urlopen", fake_urlopen)
        assert worker.ping_healthcheck("abc-123") is False

    def test_unset_uuid_skips_quietly(self, monkeypatch):
        monkeypatch.delenv("HEALTHCHECK_UUID", raising=False)

        def explode(*a, **k):
            raise AssertionError("should not ping when HEALTHCHECK_UUID is unset")

        monkeypatch.setattr(worker, "ping_healthcheck", explode)
        worker.send_dead_man_signal()

    def test_set_uuid_triggers_ping(self, monkeypatch):
        monkeypatch.setenv("HEALTHCHECK_UUID", "dead-beef")
        calls = []
        monkeypatch.setattr(worker, "ping_healthcheck", lambda u, **k: calls.append(u) or True)
        worker.send_dead_man_signal()
        assert calls == ["dead-beef"]


# ---------------------------------------------------------- run.py options


class TestRunPyOptions:
    def test_dry_run_flag(self):
        args = parse_args(["--dry-run"])
        assert args.dry_run is True

    def test_env_choices(self):
        assert parse_args(["--env", "dev"]).env == "dev"
        assert parse_args(["--env", "prod"]).env == "prod"

    def test_sources_and_tiers_parsed(self):
        args = parse_args(["--sources", "sensors,gdelt", "--tiers", "tier3"])
        assert args.sources == "sensors,gdelt"
        assert args.tiers == "tier3"

    def test_parse_tiers_names(self):
        assert parse_tiers("tier1,tier3") == [SourceTier.TIER1, SourceTier.TIER3]

    def test_parse_tiers_empty_is_all(self):
        assert parse_tiers("") is None

    def test_parse_tiers_rejects_unknown(self):
        with pytest.raises(ValueError):
            parse_tiers("tier9")

    def test_build_adapters_includes_sensors(self):
        from src.shared.config import Settings

        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:", groq_api_key="", cerebras_api_key="",
        )
        adapters = build_adapters(settings, [SourceTier.TIER3])
        names = [a.name for a in adapters]
        assert "sensors" in names
        assert "reddit_tier3" in names

    def test_build_adapters_filters_by_source_name(self):
        from src.shared.config import Settings

        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:", groq_api_key="", cerebras_api_key="",
        )
        adapters = build_adapters(settings, [SourceTier.TIER3], ["sensors"])
        assert [a.name for a in adapters] == ["sensors"]

    def test_build_adapters_filters_by_domain(self):
        from src.shared.config import Settings

        settings = Settings(
            database_url="sqlite+aiosqlite:///:memory:", groq_api_key="", cerebras_api_key="",
        )
        adapters = build_adapters(settings, [SourceTier.TIER1], ["bbc.com"])
        assert [a.name for a in adapters] == ["rss_tier1"]
