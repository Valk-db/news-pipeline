"""Tests for the CI ratchets in `ci/gates.py`.

Two things are being pinned here. First, the arithmetic and the parsing: a
ratchet that reads the wrong number is worse than no ratchet, because it looks
like a gate. Second, and more important, that the gate FAILS in every direction
it is supposed to fail in - count up, floor unreachable, output unparseable,
wrong tool version - because the failure mode this module replaced was a gate
that could not fail.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ci.gates import (
    REPO_ROOT,
    evaluate_collected,
    evaluate_mypy,
    load_baseline,
    main,
    parse_collected_count,
    parse_mypy_error_count,
    parse_mypy_version,
)

BASELINE_PATH = REPO_ROOT / "ci" / "baseline.json"

# Verbatim shapes produced by the tools, not invented ones.
# `Found 445 errors in 58 files (checked 87 source files)` is the real summary
# line measured on main (73789dc) under mypy 2.3.1, the version uv.lock pins.
MYPY_DIRTY_OUTPUT = (
    "src/reliability/consensus_analyzer.py:383: error: Argument 2 to \"where\" of "
    '"Select" has incompatible type "bool" [arg-type]\n'
    "src/reliability/consensus_analyzer.py:448: error: Argument 1 to \"where\" of "
    '"Select" has incompatible type "bool" [arg-type]\n'
    "Found 2 errors in 1 file (checked 87 source files)\n"
)
MYPY_CLEAN_OUTPUT = "Success: no issues found in 87 source files\n"
# Real pytest --collect-only -q summary lines.
COLLECTED_OUTPUT = "1899 tests collected in 6.41s\n"
COLLECTED_SINGLE_OUTPUT = "1 test collected in 0.01s\n"
COLLECTED_WITH_ERRORS_OUTPUT = "1890 tests collected, 2 errors in 3.10s\n"
COLLECTED_NONE_OUTPUT = "no tests ran in 0.05s\n"


class TestParseMypyErrorCount:
    def test_counts_the_summary_line(self) -> None:
        assert parse_mypy_error_count(MYPY_DIRTY_OUTPUT) == 2

    def test_clean_run_is_zero_not_none(self) -> None:
        # Zero and "unmeasurable" are different facts. Collapsing them would let a
        # green run through a gate that cannot tell it from a broken run.
        assert parse_mypy_error_count(MYPY_CLEAN_OUTPUT) == 0

    def test_falls_back_to_counting_error_lines(self) -> None:
        truncated = "src/a.py:1: error: something\nsrc/b.py:2: error: else\n"
        assert parse_mypy_error_count(truncated) == 2

    def test_unrecognised_output_is_none(self) -> None:
        # What a crashed mypy looks like: mypy: can't read file 'pyproject.toml'
        assert parse_mypy_error_count("mypy: can't read file 'pyproject.toml'\n") is None

    def test_empty_output_is_none(self) -> None:
        assert parse_mypy_error_count("") is None

    def test_notes_are_not_errors(self) -> None:
        noisy = (
            "src/a.py:10: note: Revealed type is 'int'\n"
            "Success: no issues found in 87 source files\n"
        )
        assert parse_mypy_error_count(noisy) == 0


class TestParseMypyVersion:
    def test_reads_the_version(self) -> None:
        assert parse_mypy_version("mypy 2.3.1 (compiled: yes)\n") == "2.3.1"

    def test_unreadable_version_is_none(self) -> None:
        assert parse_mypy_version("command not found\n") is None


class TestEvaluateMypy:
    def test_count_above_floor_fails(self) -> None:
        verdict = evaluate_mypy(MYPY_DIRTY_OUTPUT, floor=1)
        assert not verdict.ok
        assert "ROSE" in verdict.detail

    def test_count_at_floor_passes(self) -> None:
        verdict = evaluate_mypy(MYPY_DIRTY_OUTPUT, floor=2)
        assert verdict.ok
        assert verdict.exit_code == 0

    def test_count_below_floor_passes_and_asks_for_the_floor_to_drop(self) -> None:
        verdict = evaluate_mypy(MYPY_CLEAN_OUTPUT, floor=445)
        assert verdict.ok
        assert "fell" in verdict.detail
        assert "Lower mypy_error_floor" in verdict.detail

    def test_unmeasurable_output_fails(self) -> None:
        verdict = evaluate_mypy("mypy: can't read file 'pyproject.toml'\n", floor=445)
        assert not verdict.ok
        assert "could not determine" in verdict.detail

    def test_version_mismatch_fails_even_when_the_count_would_pass(self) -> None:
        # 2 errors against a floor of 445 would normally pass. It must not when
        # the counts come from different mypy versions: they are not comparable,
        # and a silent toolchain bump is how a ratchet quietly stops meaning
        # anything. Measured: main is 445 errors under 2.3.1 and 443 under 2.4.0.
        verdict = evaluate_mypy(
            MYPY_DIRTY_OUTPUT,
            floor=445,
            expected_version="2.3.1",
            actual_version="2.4.0",
        )
        assert not verdict.ok
        assert "version mismatch" in verdict.detail

    def test_matching_version_is_accepted(self) -> None:
        verdict = evaluate_mypy(
            MYPY_DIRTY_OUTPUT,
            floor=445,
            expected_version="2.3.1",
            actual_version="2.3.1",
        )
        assert verdict.ok

    def test_unreadable_running_version_fails(self) -> None:
        verdict = evaluate_mypy(
            MYPY_DIRTY_OUTPUT,
            floor=445,
            expected_version="2.3.1",
            actual_version=None,
        )
        assert not verdict.ok


class TestParseCollectedCount:
    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            (COLLECTED_OUTPUT, 1899),
            (COLLECTED_SINGLE_OUTPUT, 1),
            (COLLECTED_WITH_ERRORS_OUTPUT, 1890),
        ],
    )
    def test_reads_the_count(self, output: str, expected: int) -> None:
        assert parse_collected_count(output) == expected

    def test_no_tests_ran_is_none_not_zero(self) -> None:
        # "0 collected" would pass the floor as if the suite were empty. A suite
        # that collects nothing has not measured anything.
        assert parse_collected_count(COLLECTED_NONE_OUTPUT) is None

    def test_collection_errors_only_is_none(self) -> None:
        assert parse_collected_count("ERROR: 2 errors during collection\n") is None


class TestEvaluateCollected:
    def test_drop_below_floor_fails(self) -> None:
        verdict = evaluate_collected("1898 tests collected in 6.4s\n", floor=1899)
        assert not verdict.ok
        assert "DROPPED" in verdict.detail
        assert "justification" in verdict.detail

    def test_at_floor_passes(self) -> None:
        verdict = evaluate_collected(COLLECTED_OUTPUT, floor=1899)
        assert verdict.ok
        assert verdict.exit_code == 0

    def test_above_floor_passes_and_asks_for_the_floor_to_rise(self) -> None:
        verdict = evaluate_collected("1912 tests collected in 6.9s\n", floor=1899)
        assert verdict.ok
        assert "rose" in verdict.detail
        assert "Raise pytest_collected_floor" in verdict.detail

    def test_unparseable_output_fails(self) -> None:
        verdict = evaluate_collected("ERROR: 2 errors during collection\n", floor=1899)
        assert not verdict.ok
        assert "could not determine" in verdict.detail


@pytest.fixture(scope="module")
def baseline() -> dict:
    return load_baseline(BASELINE_PATH)


@pytest.fixture(scope="module")
def lock() -> str:
    return (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ci_workflow() -> str:
    return (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ci_workflow_steps(ci_workflow: str) -> str:
    # Comment lines are excluded on purpose: the replacement comment has to be
    # able to NAME the flag it removed without tripping the check for it.
    return "\n".join(
        line for line in ci_workflow.splitlines() if not line.lstrip().startswith("#")
    )


class TestBaselineIsUsable:
    """The baseline is the gate. If it loses a key, the gate silently disables."""

    def test_has_every_key_the_gates_read(self, baseline: dict) -> None:
        for key in (
            "mypy_error_floor",
            "mypy_targets",
            "mypy_version",
            "pytest_collected_floor",
            "pytest_collect_paths",
        ):
            assert key in baseline, f"ci/baseline.json lost {key!r}"

    def test_floors_are_plausible_integers(self, baseline: dict) -> None:
        assert isinstance(baseline["mypy_error_floor"], int)
        assert isinstance(baseline["pytest_collected_floor"], int)
        assert baseline["mypy_error_floor"] >= 0
        assert baseline["pytest_collected_floor"] > 0

    def test_records_the_commit_it_was_measured_on(self, baseline: dict) -> None:
        commit = baseline["measured_on"]["commit"]
        # A full sha, so the floor can always be traced to a tree. Not a branch
        # name: the floor has to be re-checkable after the branch is gone.
        assert re.fullmatch(r"[0-9a-f]{40}", commit), commit

    def test_pinned_mypy_version_is_the_one_uv_lock_installs(
        self, baseline: dict, lock: str
    ) -> None:
        # This is the trap the floor was nearly lost to: main is 445 errors under
        # mypy 2.3.1 and 443 under 2.4.0, so a floor measured with the wrong mypy
        # fails on arrival. If uv.lock's mypy moves, this fails until whoever
        # moved it re-measures and updates the baseline on purpose.
        locked = re.search(r'name = "mypy"\nversion = "([^"]+)"', lock)
        assert locked, "uv.lock no longer pins a version for mypy"
        assert baseline["mypy_version"] == locked.group(1)

    def test_pinned_pytest_version_is_the_one_uv_lock_installs(
        self, baseline: dict, lock: str
    ) -> None:
        # The collected count is a function of the pytest version, so a pytest
        # bump can move it without anybody removing a test.
        locked = re.search(r'name = "pytest"\nversion = "([^"]+)"', lock)
        assert locked, "uv.lock no longer pins a version for pytest"
        assert baseline["pytest_measurement"]["pytest_version"] == locked.group(1)

    def test_baseline_file_is_valid_json_with_a_readme(self, baseline: dict) -> None:
        assert isinstance(baseline["_README"], list)
        assert baseline["_README"], "an unexplained floor is a magic number"


class TestGateFailsClosed:
    def test_missing_baseline_key_exits_non_zero(self, tmp_path: Path) -> None:
        # A baseline missing the floor key must fail the gate, not default to 0
        # or to "no floor". Either default would turn the ratchet off silently.
        broken = tmp_path / "baseline.json"
        broken.write_text(json.dumps({"mypy_targets": ["src/"]}), encoding="utf-8")
        assert main(["mypy", "--baseline", str(broken)]) == 1

    def test_unreadable_baseline_exits_non_zero(self, tmp_path: Path) -> None:
        broken = tmp_path / "baseline.json"
        broken.write_text("{not json", encoding="utf-8")
        assert main(["mypy", "--baseline", str(broken)]) == 1

    def test_missing_baseline_file_exits_non_zero(self, tmp_path: Path) -> None:
        assert main(["mypy", "--baseline", str(tmp_path / "nope.json")]) == 1


class TestGateEndToEnd:
    """Drive the real CLI against the real suite.

    This is the check that the gate is wired, not merely correct in isolation.
    It costs two collection passes and no database.
    """

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ci.gates", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

    def test_committed_floor_passes_today(self) -> None:
        proc = self._run("tests")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "gate tests:" in proc.stdout

    def test_impossible_floor_exits_non_zero(self) -> None:
        # The mutation: point the gate at a floor nothing can satisfy and require
        # a non-zero exit. A gate that cannot fail is a comment with a fixture.
        proc = self._run("tests", "--floor", "100000")
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "DROPPED" in proc.stdout


class TestWorkflowWiring:
    """The workflow is text, not YAML.

    PyYAML reaches uv.lock only through the `enrichment` extra (via
    transformers), so a committed guard that parsed the workflow would be
    skipped in CI's `--extra dev --extra pipeline` environment - silently never
    running, which is the failure mode this batch exists to remove. YAML validity
    is enforced a different way: GitHub Actions refuses to load a workflow it
    cannot parse, so a malformed ci.yml takes the whole pipeline down before any
    step runs and cannot be green. What is left to assert here is the decision
    the file encodes: the type check is a ratchet, not a `continue-on-error` step.
    """

    def test_type_check_runs_the_ratchet(self, ci_workflow: str) -> None:
        assert "python -m ci.gates mypy" in ci_workflow

    def test_test_count_gate_is_wired(self, ci_workflow: str) -> None:
        assert "python -m ci.gates tests" in ci_workflow

    def test_no_step_is_marked_continue_on_error(self, ci_workflow_steps: str) -> None:
        # The specific defect being replaced: `continue-on-error: true` on the
        # mypy step with a comment claiming it only failed on new errors, and no
        # code anywhere comparing counts.
        assert "continue-on-error" not in ci_workflow_steps

    def test_raw_mypy_invocation_is_gone(self, ci_workflow_steps: str) -> None:
        assert not re.search(r"uv run mypy ", ci_workflow_steps)
