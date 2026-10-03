"""CI ratchets for the two signals that used to be unenforceable.

Both ratchets follow the same rule: a measurement that cannot be taken is a
FAILURE, never an implicit pass. Every `evaluate_*` function returns a
`Verdict` whose `ok` is False when the tool's output could not be parsed, so
a broken toolchain, a typo in the tool invocation, or a truncated output turns
the gate red instead of green.

Usage (this is how CI invokes them, with no flags beyond the gate name):

    python -m ci.gates mypy     # mypy error count must not rise above the floor
    python -m ci.gates tests    # collected test count must not fall below the floor

`--floor N` overrides the committed floor. CI never passes it; it exists so the
gate's own tests (and a human re-measuring a baseline) can point the gate at a
deliberately wrong number and watch it bite.

The floors live in `ci/baseline.json` together with the exact commit, tool
versions and commands they were measured with. Re-measure a floor only on
purpose, and record what you measured against in the same file.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "ci" / "baseline.json"

# "Found 445 errors in 58 files (checked 87 source files)" (mypy >= 0.990)
_MYPY_SUMMARY = re.compile(r"^Found (\d+) errors? in \d+ files?", re.MULTILINE)
# "Success: no issues found in 87 source files"
_MYPY_CLEAN = re.compile(r"^Success: no issues found in", re.MULTILINE)
# Fallback for builds that print errors but no summary line: "path:line: error: ..."
_MYPY_ERROR_LINE = re.compile(r": error: ")
# "1899 tests collected in 6.41s" / "1 test collected in 0.01s" /
# "1890 tests collected, 2 errors in 3.10s"
_PYTEST_COLLECTED = re.compile(r"(\d+) tests? collected")
# "mypy 2.3.1 (compiled: yes)"
_MYPY_VERSION = re.compile(r"\bmypy (\d+\.\d+\.\d+)")


@dataclass(frozen=True)
class Verdict:
    """Outcome of one gate: whether CI should fail, and the line to print."""

    ok: bool
    detail: str

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


def parse_mypy_error_count(output: str) -> int | None:
    """Number of mypy errors in `output`, or None if it cannot be determined.

    None is a real answer and callers must treat it as failure: a mypy run that
    died on a config error, was killed, or printed something unrecognised has
    not measured anything, and a gate that reads that as "fine" is the exact
    no-op this module replaced.
    """
    summary = _MYPY_SUMMARY.search(output)
    if summary:
        return int(summary.group(1))
    if _MYPY_CLEAN.search(output):
        return 0
    lines = [line for line in output.splitlines() if _MYPY_ERROR_LINE.search(line)]
    if lines:
        return len(lines)
    return None


def parse_mypy_version(output: str) -> str | None:
    """Version string out of `mypy --version` output, or None if unparseable."""
    match = _MYPY_VERSION.search(output)
    return match.group(1) if match else None


def evaluate_mypy(
    output: str,
    floor: int,
    expected_version: str | None = None,
    actual_version: str | None = None,
) -> Verdict:
    """Ratchet mypy's error count: fail when it rises, pass when it falls."""
    if expected_version and actual_version != expected_version:
        return Verdict(
            False,
            f"mypy version mismatch: baseline was measured with mypy "
            f"{expected_version}, this run is mypy {actual_version or 'unreadable'}. "
            f"Error counts are not comparable across mypy versions - re-measure "
            f"the floor in ci/baseline.json (and record the new version).",
        )
    count = parse_mypy_error_count(output)
    if count is None:
        return Verdict(
            False,
            "could not determine the mypy error count (no 'Found N errors' summary "
            "and no ': error:' lines). Treating an unmeasurable gate as a failure.",
        )
    if count > floor:
        return Verdict(
            False,
            f"mypy error count ROSE: {count} errors, floor is {floor} "
            f"({count - floor} over). Fix the new errors or, if the floor was "
            f"measured against a different mypy version, re-measure it deliberately.",
        )
    if count < floor:
        return Verdict(
            True,
            f"mypy error count fell: {count} errors, floor is {floor}. "
            f"Lower mypy_error_floor in ci/baseline.json to {count} so the "
            f"improvement cannot be silently given back.",
        )
    return Verdict(True, f"mypy error count unchanged: {count} errors, floor is {floor}.")


def parse_collected_count(output: str) -> int | None:
    """Tests collected per `pytest --collect-only -q`, or None if unparseable."""
    match = _PYTEST_COLLECTED.search(output)
    return int(match.group(1)) if match else None


def evaluate_collected(output: str, floor: int) -> Verdict:
    """Floor on collected tests: fail when a test disappears."""
    count = parse_collected_count(output)
    if count is None:
        return Verdict(
            False,
            "could not determine the collected test count (no 'N tests collected' "
            "line). Collection errors or a broken pytest can produce this - "
            "treating an unmeasurable gate as a failure.",
        )
    if count < floor:
        return Verdict(
            False,
            f"collected test count DROPPED: {count} tests collected, floor is {floor} "
            f"({floor - count} missing). A removed test needs a named justification "
            f"in the batch report; if it is justified, lower pytest_collected_floor "
            f"in ci/baseline.json on purpose.",
        )
    if count > floor:
        return Verdict(
            True,
            f"collected test count rose: {count} tests collected, floor is {floor}. "
            f"Raise pytest_collected_floor in ci/baseline.json to {count} so a later "
            f"removal is measured against the real suite.",
        )
    return Verdict(True, f"collected test count unchanged: {count} tests, floor is {floor}.")


def load_baseline(path: Path) -> dict:
    """Read the committed baseline. Raises loudly rather than inventing a floor."""
    with path.open(encoding="utf-8") as handle:
        baseline = json.load(handle)
    if not isinstance(baseline, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return baseline


def _floor(baseline: dict, key: str, override: int | None) -> int:
    if override is not None:
        return override
    if key not in baseline:
        raise ValueError(f"baseline has no {key!r} key")
    return int(baseline[key])


def _run(argv: list[str]) -> tuple[int, str]:
    """Run a command, returning (returncode, combined output)."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def gate_mypy(baseline: dict, override: int | None = None) -> Verdict:
    """Run the configured mypy targets and ratchet the error count."""
    floor = _floor(baseline, "mypy_error_floor", override)
    targets = baseline.get("mypy_targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("baseline has no 'mypy_targets' list")
    expected = baseline.get("mypy_version")
    version = None
    if expected:
        _, version_output = _run([sys.executable, "-m", "mypy", "--version"])
        version = parse_mypy_version(version_output)
    _, output = _run([sys.executable, "-m", "mypy", *targets])
    return evaluate_mypy(output, floor, expected_version=expected, actual_version=version)


def gate_tests(baseline: dict, override: int | None = None) -> Verdict:
    """Run `pytest --collect-only` over the suite and ratchet the count."""
    floor = _floor(baseline, "pytest_collected_floor", override)
    paths = baseline.get("pytest_collect_paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("baseline has no 'pytest_collect_paths' list")
    _, output = _run(
        [sys.executable, "-m", "pytest", *paths, "--collect-only", "-q"],
    )
    return evaluate_collected(output, floor)


GATES = {"mypy": gate_mypy, "tests": gate_tests}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=sorted(GATES))
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help="committed floor file (default: ci/baseline.json)",
    )
    parser.add_argument(
        "--floor",
        type=int,
        default=None,
        help="override the committed floor; used by the gate's own tests",
    )
    args = parser.parse_args(argv)
    try:
        baseline = load_baseline(args.baseline)
        verdict = GATES[args.gate](baseline, args.floor)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # A baseline we cannot read, or a baseline missing a required key, is a
        # gate failure. Defaulting to "no floor" here would silently disable it.
        print(f"gate {args.gate}: BASELINE ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"gate {args.gate}: {verdict.detail}")
    return verdict.exit_code


if __name__ == "__main__":
    raise SystemExit(main())