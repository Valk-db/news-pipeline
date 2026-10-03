"""Compare a measured number against the floor recorded in ci_quality_floor.json.

CI used to run `mypy` with `continue-on-error: true` and the comment
"Allow pre-existing errors, only fail on new ones" -- except it did not fail on
new ones either, because nothing was comparing the count to anything. The
comment described an intention the workflow did not implement: a green mypy
step, forever, no matter how many errors were added. This module is the missing
half of that intention.

It is a ratchet, not a pass/fail switch. mypy on this tree is nowhere near
clean, so turning it into a hard gate would only mean deleting the step. What
can be enforced is "no worse than the number we recorded", which stops the debt
from growing while leaving the fixing to whoever is already in the file. The
pytest side is a floor for the same reason: a suite can be green while
somebody has deleted the tests that were failing.

Usage:

    uv run mypy src/ curation_ui/ > /tmp/mypy.txt; \\
        uv run python -m scripts.ci_quality_gate mypy /tmp/mypy.txt

    uv run pytest tests/ --collect-only -q > /tmp/collected.txt; \\
        uv run python -m scripts.ci_quality_gate pytest /tmp/collected.txt

The producing command's own exit status is deliberately ignored by the caller
(mypy exits 1 whenever it finds anything, which is the normal case here). So
this module must never infer "fine" from an output file it could not read: if
the number cannot be extracted it exits 2, loudly. A gate that passes when it
cannot see what it is gating is worse than no gate, because it reports green.

Exit codes: 0 within the floor, 1 outside it, 2 could not measure.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FLOOR_FILE = Path(__file__).resolve().parent.parent / "ci_quality_floor.json"

# mypy's own summary line. Plural is irregular in mypy's output: "Found 1 error
# in 1 file", "Found 441 errors in 56 files (checked 86 source files)".
_MYPY_SUMMARY = re.compile(
    r"^Found (?P<errors>\d+) errors?(?: in (?P<files>\d+) files?)?"
    r"(?: \(checked (?P<checked>\d+) source files?\))?$"
)
_MYPY_CLEAN = re.compile(r"^Success: no issues found in (?P<checked>\d+) source files?$")

# `pytest --collect-only -q` ends in "1827 tests collected in 11.49s", and
# "1 test collected" when a single test is selected.
_PYTEST_COLLECTED = re.compile(r"^(?P<count>\d+) tests? collected(?: in .*)?$")


class Unmeasurable(Exception):
    """The output did not contain the number this gate needs."""


def load_floor(path: Path = FLOOR_FILE) -> dict:
    """Read the recorded floors. Raises rather than defaulting: a missing or
    malformed floor file must not silently disable the gate."""
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _last_matching_line(text: str, pattern: re.Pattern[str]) -> re.Match[str] | None:
    for line in reversed(text.splitlines()):
        match = pattern.match(line.strip())
        if match:
            return match
    return None


def parse_mypy_errors(text: str) -> int:
    """Error count from mypy's stdout/stderr. Zero when mypy reported success.

    mypy writes its summary to stdout, so the caller must capture both streams.
    """
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if _MYPY_CLEAN.match(stripped):
            return 0
        match = _MYPY_SUMMARY.match(stripped)
        if match:
            return int(match.group("errors"))
    raise Unmeasurable(
        "no mypy summary line in the captured output. mypy prints "
        "'Found N errors in M files (checked K source files)' or "
        "'Success: no issues found in N source files' as its last line; if it "
        "is missing, mypy crashed rather than type-checked, and treating that as "
        "a pass would report green for a run that checked nothing"
    )


def parse_pytest_collected(text: str) -> int:
    """Number of tests `pytest --collect-only -q` reported collecting."""
    match = _last_matching_line(text, _PYTEST_COLLECTED)
    if match:
        return int(match.group("count"))
    raise Unmeasurable(
        "no 'N tests collected' line in the captured output. If collection "
        "errored, pytest exits before printing a summary and the run itself is "
        "already red; if it printed nothing at all, there is nothing to floor"
    )


def check_mypy(text: str, floor: dict) -> tuple[bool, str]:
    measured = parse_mypy_errors(text)
    allowed = int(floor["mypy"]["max_errors"])
    ok = measured <= allowed
    slack = allowed - measured
    return ok, (
        f"mypy: {measured} error(s), floor is {allowed} "
        f"({slack} of headroom)"
        if ok
        else (
            f"mypy: {measured} errors, floor is {allowed}. "
            f"{measured - allowed} new error(s) since the floor was recorded. "
            f"Fix them, or -- if they are pre-existing and unrelated to your "
            f"change -- say so in the PR and record why in "
            f"ci_quality_floor.json. Never raise the floor to make a build green."
        )
    )


def check_pytest(text: str, floor: dict) -> tuple[bool, str]:
    measured = parse_pytest_collected(text)
    required = int(floor["pytest"]["min_collected"])
    ok = measured >= required
    return ok, (
        f"pytest: {measured} collected, floor is {required}"
        if ok
        else (
            f"pytest: {measured} collected, floor is {required}. "
            f"{required - measured} test(s) fewer than the recorded floor. If you "
            f"deleted tests on purpose, say which ones and why in the commit "
            f"message, and lower min_collected in the same commit. If you did "
            f"not, this is a collection error and the failure is above you."
        )
    )


CHECKS = {"mypy": (parse_mypy_errors, check_mypy), "pytest": (parse_pytest_collected, check_pytest)}


USAGE = "usage: python -m scripts.ci_quality_gate {mypy|pytest} <captured-output-file>"


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in CHECKS:
        print(USAGE, file=sys.stderr)
        return 2
    which, output_path = argv[1], Path(argv[2])
    try:
        text = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"could not read {output_path}: {exc}", file=sys.stderr)
        return 2
    try:
        ok, message = CHECKS[which][1](text, load_floor())
    except Unmeasurable as exc:
        print(f"GATE COULD NOT RUN: {exc}", file=sys.stderr)
        return 2
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
