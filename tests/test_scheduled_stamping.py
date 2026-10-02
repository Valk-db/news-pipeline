"""Guards on the scheduled transparency-stamping path.

The bug this file exists to prevent: stamp_observations() in
src/ingestion/rss_evidence.py is the only code path that appends to
merkle_log_entries and sets raw_articles.log_index, and it is reachable only
through RssEvidenceAdapter, which is opt-in behind --sources. A unit suite
proves the adapter stamps, and the /proof permalink suite proves the page
renders, and the production log still never grows, because nothing scheduled
ever asks for the adapter. Tests cannot see a cron entry point.

So these tests read the workflow files themselves and assert that a scheduled
run actually invokes the stamping path, and that it does so before the
checkpoint signer runs. Both are cheap, deterministic, and they fail the moment
the wiring is removed.

Deliberately no YAML parser: pyyaml is not a declared test dependency (it is
only a transitive entry in uv.lock) and must not become one for a guard. Plain
regex over the workflow text is enough for the two facts being asserted, and
cannot fail to import in CI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# `python -m src.ingestion.run` with the evidence locker selected, in whatever
# spelling the workflow uses for it: separate tokens, comma-joined, or trailing
# flag with the value on the next line.
STAMPING_COMMAND_RE = re.compile(
    r"src\.ingestion\.run[^\n#]*?--sources[= ]+[\"']?([A-Za-z0-9_,]+)",
)
SCHEDULE_RE = re.compile(r"schedule:\s*\n\s*-\s*(?:cron:\s*)?[\"']?([^\"'\n#]+)", re.MULTILINE)
ANY_SCHEDULE_RE = re.compile(r"^\s*schedule:\s*$", re.MULTILINE)


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))


def _workflow_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cron_field(spec: str, index: int) -> set[int]:
    """Expand one comma-separated cron field into the set of values it covers."""
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(60) if index == 0 else range(24))
            continue
        values.add(int(part))
    return values


def _cron_windows(spec: str) -> list[tuple[int, int]]:
    """Minute-of-day windows for a daily cron spec (minute hour dom month)."""
    fields = spec.split()
    assert len(fields) == 5, f"not a 5-field cron: {spec!r}"
    minute, hour, dom, month = fields[0], fields[1], fields[2], fields[3]
    # A day-of-month or month restriction means "not every day", and the
    # ordering guarantee this module asserts only holds for a daily cron.
    assert dom == "*" and month == "*", f"cron is not daily: {spec!r}"
    return sorted((h * 60 + m) for m in _cron_field(minute, 0) for h in _cron_field(hour, 1))


def _checkpoint_cron_minutes() -> list[int]:
    config = json.loads((REPO_ROOT / "vercel.json").read_text(encoding="utf-8"))
    checkpoint = [
        entry["schedule"]
        for entry in config.get("crons", [])
        if entry["path"] == "/api/cron/checkpoint"
    ]
    assert len(checkpoint) == 1, f"expected exactly one checkpoint cron, found {checkpoint}"
    return _cron_windows(checkpoint[0])


def _stamping_workflows() -> list[tuple[Path, str, str]]:
    """(path, raw command fragment, cron spec) for every scheduled stamping run."""
    found = []
    for path in _workflow_files():
        text = _workflow_text(path)
        if not ANY_SCHEDULE_RE.search(text):
            continue
        for match in STAMPING_COMMAND_RE.finditer(text):
            if "rss_evidence" in match.group(1).split(","):
                crons = SCHEDULE_RE.findall(text)
                assert crons, f"{path.name} stamps on a schedule it does not declare"
                found.append((path, match.group(0).strip(), crons[0].strip()))
    return found


def test_a_scheduled_workflow_runs_the_stamping_adapter():
    """A cron-triggered run must invoke the only code path that stamps."""
    found = _stamping_workflows()
    assert found, (
        "no scheduled workflow runs `python -m src.ingestion.run --sources rss_evidence`. "
        "The evidence locker is opt-in, so unless a cron asks for it the merkle log "
        "never grows and every ingested article renders /proof as pending forever."
    )


def test_stamping_workflow_targets_the_same_script_as_the_daily_ingest():
    """The stamping step must call src.ingestion.run, not some other entry point."""
    found = _stamping_workflows()
    assert found
    for path, command, _cron in found:
        assert "src.ingestion.run" in command, f"{path.name}: {command}"


def test_stamping_runs_before_the_checkpoint_signer():
    """Stamps must land before the signer signs, or every signed tree lags a day."""
    checkpoint = _checkpoint_cron_minutes()
    found = _stamping_workflows()
    assert found, "no scheduled workflow runs the stamping adapter; ordering is unprovable"
    for path, _command, cron in found:
        windows = _cron_windows(cron)
        assert windows, f"{path.name}: unparseable cron {cron!r}"
        for stamp_at in windows:
            for sign_at in checkpoint:
                assert stamp_at < sign_at, (
                    f"{path.name} stamps at minute-of-day {stamp_at} but "
                    f"/api/cron/checkpoint signs at {sign_at}; the signed tree "
                    f"would be a day behind the log"
                )


def test_every_scheduled_workflow_declares_a_bounded_timeout():
    """A stamping run that hangs must not outlive its job.

    The locker polls four feeds sequentially and extracts up to ten bodies, each
    with its own timeout, so a pathological run is minutes, not hours. A missing
    timeout-minutes means GitHub's default (360m) applies and a hung run holds
    the concurrency group.
    """
    for path in _workflow_files():
        if not ANY_SCHEDULE_RE.search(_workflow_text(path)):
            continue
        assert "timeout-minutes:" in _workflow_text(path), f"{path.name} has no timeout-minutes"
