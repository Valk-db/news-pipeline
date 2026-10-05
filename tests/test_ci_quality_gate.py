"""The CI quality gate has to actually gate, and has to keep gating.

The failure this file exists to prevent is specific and already happened once:
`mypy` ran in CI with `continue-on-error: true` and a comment claiming it
"only fail[s] on new ones", while nothing in the job compared the error count
to anything. The step was structurally incapable of failing and looked like a
gate. So these tests parse .github/workflows/ci.yml as YAML and assert the
wiring, rather than trusting the step names to describe themselves.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.ci_quality_gate import (
    Unmeasurable,
    check_mypy,
    check_pytest,
    load_floor,
    parse_mypy_errors,
    parse_pytest_collected,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    # Parsed, not grepped. A grep for "continue-on-error" would also match the
    # comment explaining why it is gone, and a grep for a step name would match
    # a step that has been renamed into meaninglessness.
    with WORKFLOW.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _steps(workflow: dict, job: str) -> list[dict]:
    return [s for s in workflow["jobs"][job]["steps"]]


def _step(workflow: dict, job: str, name: str) -> dict:
    matches = [s for s in _steps(workflow, job) if s.get("name") == name]
    assert len(matches) == 1, f"expected exactly one {job} step named {name!r}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------- the wiring


def test_mypy_step_is_not_allowed_to_fail(workflow: dict) -> None:
    step = _step(workflow, "lint", "Ratchet mypy error count")
    assert "continue-on-error" not in step, (
        "continue-on-error on the mypy step is what made the old gate a no-op; "
        "the comparison against ci_quality_floor.json is the gate now"
    )


def test_mypy_step_ends_by_comparing_against_the_floor(workflow: dict) -> None:
    run = _step(workflow, "lint", "Ratchet mypy error count")["run"]
    assert "ci.gates mypy" in run, run
    # The gate compares the error count against the floor in ci/baseline.json;
    # the step must not have continue-on-error (checked separately).


def test_the_whole_lint_job_cannot_pass_over_a_type_error(workflow: dict) -> None:
    for job in ("lint", "test"):
        for step in _steps(workflow, job):
            assert "continue-on-error" not in step, f"{job} step {step.get('name')!r} can fail silently"


def test_both_jobs_actually_run_their_checks(workflow: dict) -> None:
    lint_run = "\n".join(s.get("run", "") for s in _steps(workflow, "lint"))
    test_run = "\n".join(s.get("run", "") for s in _steps(workflow, "test"))
    assert "ruff check ." in lint_run
    assert "ci.gates mypy" in lint_run
    assert "detect-secrets scan" in lint_run
    assert "pytest tests/" in test_run
    assert "scripts.ci_quality_gate pytest" in test_run


def test_the_floor_check_runs_in_the_job_that_has_a_database(workflow: dict) -> None:
    # Collection can depend on the environment; this floor was measured without
    # one, so the step must be as environment-independent as it can be.
    step = _step(workflow, "test", "Check the test-count floor")
    assert "DATABASE_URL" in step.get("env", {})


# ------------------------------------------------------------- the recording


def test_the_floor_file_is_readable_and_complete() -> None:
    floor = load_floor()
    assert isinstance(floor["mypy"]["max_errors"], int)
    assert isinstance(floor["pytest"]["min_collected"], int)
    assert floor["mypy"]["max_errors"] >= 0
    assert floor["pytest"]["min_collected"] > 0
    # The rationale travels with the number, or the next person raises the
    # floor without knowing it is a ratchet.
    for section in ("mypy", "pytest"):
        rationale = [v for k, v in floor[section].items() if "why" in k]
        assert rationale and rationale[0].strip(), f"{section} has no recorded rationale"


def test_the_recorded_floor_still_admits_this_tree() -> None:
    # Cheap relative to a mypy run, and it turns "the floor file says 445" into
    # a claim that is checked rather than remembered.
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "src/", "curation_ui/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    measured = parse_mypy_errors(proc.stdout + proc.stderr)
    assert measured <= load_floor()["mypy"]["max_errors"], (
        f"this tree has {measured} mypy errors, over the recorded floor of "
        f"{load_floor()['mypy']['max_errors']}; fix them or record why"
    )


# ------------------------------------------------------------- the parsers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Found 441 errors in 56 files (checked 86 source files)", 441),
        ("Found 1 error in 1 file (checked 1 source file)", 1),
        ("Success: no issues found in 86 source files", 0),
        ("src/a.py:1: error: X [misc]\nFound 2 errors in 1 file (checked 86 source files)", 2),
        # Extra output after the summary (a tail, a warning) must not confuse it.
        ("Found 3 errors in 2 files (checked 86 source files)\nwarning: something", 3),
    ],
)
def test_mypy_error_counts_are_read_off_the_summary_line(text: str, expected: int) -> None:
    assert parse_mypy_errors(text) == expected


def test_a_crashed_mypy_is_not_a_passing_mypy() -> None:
    with pytest.raises(Unmeasurable):
        parse_mypy_errors("mypy: can't read file 'src/nope.py': No such file or directory")


def test_an_empty_capture_is_not_a_passing_mypy() -> None:
    with pytest.raises(Unmeasurable):
        parse_mypy_errors("")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1827 tests collected in 11.49s", 1827),
        ("1 test collected in 0.01s", 1),
        ("tests/test_a.py::test_x\ntests/test_b.py::test_y\n\n1827 tests collected in 11.49s", 1827),
    ],
)
def test_collected_counts_are_read_off_the_pytest_summary(text: str, expected: int) -> None:
    assert parse_pytest_collected(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no tests ran in 0.02s",
        # A collection error prints the reason and no summary. The pytest run
        # above already failed for this; the gate must not also read it as zero
        # collected and cry about the floor.
        "ERROR: collecting tests/test_broken.py\nInterrupted: 1 error during collection",
    ],
)
def test_an_unmeasurable_pytest_output_is_never_zero_collected(text: str) -> None:
    with pytest.raises(Unmeasurable):
        parse_pytest_collected(text)


# ------------------------------------------------------------- the verdicts


def test_mypy_under_the_floor_passes_with_headroom_reported() -> None:
    ok, message = check_mypy("Found 1 error in 1 file (checked 86 source files)", load_floor())
    assert ok
    assert "445" in message and "headroom" in message


def test_mypy_over_the_floor_fails_and_says_so() -> None:
    ok, message = check_mypy("Found 446 errors in 57 files (checked 86 source files)", load_floor())
    assert not ok
    assert "446" in message and "445" in message
    assert "Never raise the floor to make a build green." in message


def test_mypy_exactly_at_the_floor_passes() -> None:
    ok, _ = check_mypy("Found 445 errors in 56 files (checked 86 source files)", load_floor())
    assert ok


def test_pytest_under_the_floor_fails_and_names_the_shortfall() -> None:
    ok, message = check_pytest("1800 tests collected in 10s", load_floor())
    assert not ok
    assert "1800" in message and "1816" in message
    assert "16 test(s) fewer" in message


def test_pytest_at_or_over_the_floor_passes() -> None:
    assert check_pytest("1816 tests collected in 10s", load_floor())[0]
    assert check_pytest("1827 tests collected in 10s", load_floor())[0]
