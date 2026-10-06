"""`scripts/check_free_models.py` was run by nobody, and a workflow that is not
wired is a comment.

The script has been in the tree since a8255ef and no workflow invoked it. The
failure it guards against is invisible without it: with a fallback chain in front
of the roster, a retired `:free` id produces a 404 on every request to that rung,
which reads exactly like "the free pool is busy today". The chain falls through,
nobody is told why, and the rung is demoted for the run for a reason that will
still be true tomorrow.

The wiring decision this file pins is three-valued, because the script is. The
obvious wirings are both wrong:

  * FAIL on any non-zero exit and an OpenRouter 502 turns the workflow red on a
    day nobody can act on, so people learn to ignore it;
  * WARN on any non-zero exit and a genuinely retired id -- the one case that
    needs a human, and needs one within a day rather than within the two weeks a
    silently ignored warning survives -- looks identical to a catalogue hiccup.

So: exit 1 (confirmed stale) fails the job, exit 2 (could not check) does not fail
but must be loud, and "learned nothing" may never render as "everything is fine".
The last test in this file is the one that matters: it EXECUTES the step's shell
body, because a guard that reads a config file as text is not a guard that the
config file works. That is how the stamping-wire batch's real defects got through
-- a report script that raised before printing a number, on every run, in the
target.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "free-model-check.yml"
SCRIPT = REPO_ROOT / "scripts" / "check_free_models.py"


@pytest.fixture(scope="module")
def workflow() -> dict:
    # Parsed, not grepped. A grep for "schedule" also matches the comment that
    # explains why there is no pull_request trigger, and a grep for a step name
    # matches a step renamed into meaninglessness.
    with WORKFLOW.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _job(workflow: dict) -> dict:
    jobs = workflow["jobs"]
    assert len(jobs) == 1, (
        "expected one job, found %s. A second job would mean a second opinion on "
        "what a failed check means, and the reason this workflow has exactly one "
        "job is that there is exactly one verdict." % sorted(jobs)
    )
    return next(iter(jobs.values()))


def _steps(workflow: dict) -> list[dict]:
    return list(_job(workflow)["steps"])


def _step(workflow: dict, fragment: str) -> dict:
    matches = [s for s in _steps(workflow) if fragment in (s.get("name") or "")]
    assert len(matches) == 1, (
        "expected exactly one step whose name contains %r, found %d: %s"
        % (fragment, len(matches), [s.get("name") for s in _steps(workflow)])
    )
    return matches[0]


def _runs(workflow: dict) -> str:
    return "\n".join(s.get("run", "") for s in _steps(workflow))


# ------------------------------------------------------------------ the trigger


def test_it_is_scheduled_and_manually_runnable(workflow: dict) -> None:
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "schedule" in triggers, (
        "the check has no schedule, so a retired free id is discovered by the run "
        "that fails instead of by the check"
    )
    assert "workflow_dispatch" in triggers, (
        "no workflow_dispatch: after editing a model id you would have to wait for "
        "the next Monday rather than checking it now"
    )


def test_it_does_not_gate_pull_requests(workflow: dict) -> None:
    """A third-party dependency does not belong in the merge path.

    This is the only check in the repo that needs somebody else's API to be up. If
    it fires on pull_request, mergeability becomes a function of OpenRouter's
    uptime and the first person to hit a 502 on a Friday afternoon adds
    `continue-on-error`, which deletes the check.
    """
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "pull_request" not in triggers, (
        "the free-model check is running on pull_request. It is the only check here "
        "that depends on a third party being reachable."
    )
    assert "push" not in triggers, (
        "the check is running on every push, which turns an external service into a "
        "merge gate by the back door"
    )


def test_the_schedule_does_not_collide_with_a_scheduled_pipeline(workflow: dict) -> None:
    """Measured against the other workflows, not asserted in the abstract.

    A staleness check that fires at the same minute as the ingest it is meant to
    protect is a check whose report is read while the thing it is about is already
    running. The other schedules, read off the workflow files:
    daily-ingest `23 6,18 * * *`, transparency-stamp `47 4 * * *`,
    weekly-enrichment `23 2 * * 0`, cleanup `23 3 * * 0`, daily-phase2
    `23 20 * * *`.
    """
    taken = {"23 6,18 * * *", "47 4 * * *", "23 2 * * 0", "23 3 * * 0", "23 20 * * *"}
    triggers = workflow[True] if True in workflow else workflow["on"]
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert crons, "schedule is present but empty"
    for cron in crons:
        assert cron not in taken, "cron %r collides with a scheduled pipeline" % cron
    # And it must land BEFORE the first scheduled run that would consume a stale
    # rung, so the roster is fixed before the day's ingest rather than after it.
    # A cron field order is "min hour dom mon dow", so index 0 is the minute.
    # Getting this backwards is the kind of thing that makes a timing assertion
    # pass for the wrong reason.
    ingest = ["23 6", "18 * * *"][0].split()
    minute, hour = int(crons[0].split()[0]), int(crons[0].split()[1])
    assert (hour, minute) < (int(ingest[1]), int(ingest[0])), (
        "the check runs at %02d:%02d, which is not before the %02d:%02d daily "
        "ingest. The report would be read after the run that uses the stale id."
        % (hour, minute, int(ingest[1]), int(ingest[0]))
    )


# ----------------------------------------------------------------- the wiring


def test_the_script_is_actually_invoked(workflow: dict) -> None:
    run = _runs(workflow)
    assert "scripts.check_free_models" in run, (
        "the workflow exists but never runs the script: %s" % run
    )


def test_the_key_comes_from_a_secret_and_is_not_hardcoded(workflow: dict) -> None:
    check = _step(workflow, "live catalogue")
    env = check.get("env") or {}
    assert env.get("OPENROUTER_API_KEY") == "${{ secrets.OPENROUTER_API_KEY }}", (
        "OPENROUTER_API_KEY must come from the Actions secret, not from the "
        "environment and not as a literal: %r" % env
    )
    # No OpenRouter-shaped key literal anywhere in the file, including the comments
    # and the echo bodies: a pasted key is committed, and an `echo` of the env puts
    # it in the step log where anyone with read access can see it. Matching on the
    # key's own shape rather than on the variable name, because the name legitimately
    # appears in prose that explains why the run is non-blocking.
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets.OPENROUTER_API_KEY" in raw
    assert "sk-or-" not in raw and "sk-or-v1-" not in raw, (
        "an OpenRouter-shaped key literal reached .github/workflows/"
        "free-model-check.yml. Revoke it and rotate the secret."
    )
    # The value must never be interpolated into a command, and the step must never
    # trace itself. Both would land the key in the step log, which anyone with read
    # access to the repository can read. Matching on the interpolation rather than on
    # the variable NAME, because the name legitimately appears in an echo that says
    # "no key is configured", and a test that forbids that is a test that gets
    # deleted to make a warning go away.
    runs = _runs(workflow)
    assert "$OPENROUTER_API_KEY" not in runs and "${OPENROUTER_API_KEY" not in runs, (
        "a run body interpolates $OPENROUTER_API_KEY, so the key would be expanded "
        "into a command line and land in the step log"
    )
    assert "set -x" not in runs and "set +x" not in runs, (
        "a run body enables shell tracing. `set -x` echoes every expanded command, "
        "which is how a dev DB password was leaked into a log on 2026-10-02."
    )
    assert "printenv" not in runs and not re.search(r"\benv\s*\|", runs), (
        "a run body dumps the environment, which would print the key and the egress "
        "proxy credential into the log"
    )
    # And the one assignment of the name is the step's env, which the assertion at
    # the top of this test already pinned to the secret expression. Asserting that
    # there is no assignment at all would fail on that correct line, which is a test
    # that gets deleted rather than fixed.
    assert [ln for ln in raw.splitlines()
            if re.match(r"\s*OPENROUTER_API_KEY\s*:", ln)] == [
        "          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}"], (
        "the name is assigned somewhere other than the check step's env block"
    )


def test_the_catalogue_is_fetched_once_and_the_output_is_captured(
        workflow: dict) -> None:
    """One read, one report. Two reads could disagree with each other.

    The free list rotates, so a second read is a second sample of a moving thing,
    and a report assembled from two samples is a report that can describe a roster
    state that never existed.
    """
    run = _step(workflow, "live catalogue")["run"]
    assert run.count("scripts.check_free_models") == 1, (
        "the catalogue is read %d times; the verdict and the report must come from "
        "the same read" % run.count("scripts.check_free_models")
    )
    assert "set +e" in run, (
        "the check step lets a non-zero exit end the step, so the step that names a "
        "retired id never runs"
    )
    assert "GITHUB_OUTPUT" in run, (
        "the exit code is never published as a step output, so the steps that branch "
        "on it cannot branch on it"
    )


def test_a_confirmed_retired_id_fails_the_job(workflow: dict) -> None:
    """Exit 1 is the actionable case and it is the only one that fails."""
    step = _step(workflow, "fails the job")
    assert step.get("if") == "steps.free_models.outputs.exit_code == '1'", step.get("if")
    assert "exit 1" in step["run"], step["run"]
    assert "::error::" in step["run"], "a failing step should say why in the log"
    assert "llm_roster" in step["run"], (
        "the failure must name the file to edit, or the next reader has to go find it"
    )


def test_could_not_check_does_not_fail_but_is_not_silent(workflow: dict) -> None:
    """Exit 2 must be visible. It is allowed not to be red, and nothing else."""
    step = _step(workflow, "is not a pass")
    assert step.get("if") == "steps.free_models.outputs.exit_code == '2'", step.get("if")
    assert "exit 1" not in step["run"], (
        "the could-not-check step fails the job. An OpenRouter 502 would then be a "
        "red workflow nobody can act on, which is how a check learns to be ignored."
    )
    assert "::warning" in step["run"], (
        "a could-not-check run emits no annotation, so it is a green tick that means "
        "nothing at all"
    )
    assert "NOT evidence" in step["run"], (
        "the warning must say the run established nothing, or a reader takes a "
        "warning run as a clean one"
    )


def test_the_report_path_is_real(workflow: dict) -> None:
    """A log line in a scrollback nobody opens is not a report.

    Two destinations, because they fail differently: the job summary is what a
    human sees when they open the run, and the artifact is what still exists a month
    later when somebody asks what the free list looked like on the 12th.
    """
    run = _step(workflow, "live catalogue")["run"]
    assert "GITHUB_STEP_SUMMARY" in run, (
        "nothing is written to the job summary, so the only record is the step log"
    )
    assert "cat /tmp/free-models.txt" in run, (
        "the summary does not carry the script's output, so the summary says the "
        "exit code and not what was found"
    )
    uploads = [s for s in _steps(workflow) if "upload-artifact" in (s.get("uses") or "")]
    assert len(uploads) == 1, "expected exactly one artifact upload"
    assert uploads[0]["with"]["path"] == "/tmp/free-models.txt", (
        "the artifact uploads %r rather than the captured output"
        % uploads[0]["with"].get("path")
    )
    assert int(uploads[0]["with"]["retention-days"]) >= 7, (
        "a retention window under a week does not survive long enough to be compared "
        "against the previous run"
    )


def test_nothing_in_the_workflow_can_report_a_pass_it_did_not_earn(
        workflow: dict) -> None:
    """A missing exit_code is "learned nothing", and must not render as green.

    The step's `id` output is absent if the check step itself never ran (a failed
    checkout, a cancelled run). Two `if: == '1'` and `if: == '2'` steps then both
    skip, the job is green, and the roster was never looked at.
    """
    guard = _step(workflow, "published no verdict")
    assert "always()" in guard.get("if", ""), guard.get("if")
    assert "exit 1" in guard["run"], "the missing-output case does not fail the job"
    assert "::error::" in guard["run"], guard["run"]


# --------------------------------------------------- executing the step, for real


def _step_shell(workflow: dict) -> str:
    """The check step's body, with `uv run` swapped for the interpreter here.

    `uv` is not on PATH on this machine, so the body is run with the same Python
    that runs this suite. Everything else -- `set +e`, the capture, the summary, the
    GITHUB_OUTPUT append -- is the shell's, unchanged, which is the part that can be
    wrong in a way a YAML parse cannot see.
    """
    run = _step(workflow, "live catalogue")["run"]
    return run.replace("uv run python -m scripts.check_free_models",
                       f"{sys.executable} -m scripts.check_free_models")


def test_the_step_body_actually_runs_and_publishes_its_exit_code(
        workflow: dict) -> None:
    """The load-bearing test: execute the shell, do not read it.

    The script exits 2 when no key is configured, which is the honest default for a
    fork or a keyless repository. So this is the branch that runs most often, and it
    is the branch where `set -e` in the wrong place turns "could not check" into a
    red job and "a captured output" into "an empty file".
    """
    shell = _step_shell(workflow)
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.md"
        output = Path(tmp) / "output.txt"
        env = {
            # Inherited minus the two variables that would leak the egress proxy
            # credential into this test's output, and minus any key, on purpose.
            k: v for k, v in os.environ.items()
            if k not in ("OPENROUTER_API_KEY", "NO_PROXY", "no_proxy")
        }
        env["GITHUB_STEP_SUMMARY"] = str(summary)
        env["GITHUB_OUTPUT"] = str(output)
        env["NO_PROXY"] = env["no_proxy"] = ""
        proc = subprocess.run(["bash", "-c", shell], cwd=str(REPO_ROOT), env=env,
                              capture_output=True, text=True, timeout=180)
        # Read inside the `with`: a TemporaryDirectory is gone by the time the
        # assertions run, and asserting on a deleted path fails with FileNotFound
        # rather than with anything about the workflow.
        published = output.read_text(encoding="utf-8") if output.exists() else ""
        body = summary.read_text(encoding="utf-8") if summary.exists() else ""

    assert proc.returncode == 0, (
        "the step body exited %d, so the job dies before the step that names a "
        "retired id can run. stdout:\n%s\nstderr:\n%s"
        % (proc.returncode, proc.stdout[-2000:], proc.stderr[-2000:])
    )
    assert "exit_code=2" in published, (
        "the step did not publish exit_code=2; GITHUB_OUTPUT holds %r" % published
    )

    assert "OPENROUTER_API_KEY is not set" in body, (
        "the job summary does not carry the script's output. It holds:\n%s" % body
    )
    assert "exit code: `2`" in body, (
        "the job summary does not state the exit code in words, so a reader cannot "
        "tell a clean run from a run that learned nothing"
    )
    assert "NOT evidence" not in body, (
        "the summary for a could-not-check run must not read like a clean one"
    )


def test_the_script_really_is_three_valued() -> None:
    """The workflow branches on three exit codes, so the script must have three.

    Read off the script rather than trusted: the branching assertions above are
    only meaningful if 0/1/2 mean what the comments say. A script that returned 1
    for "catalogue unreachable" as well as for "id retired" would make the
    could-not-check branch dead code and the job red on every hiccup.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert "return 0" in source and "return 1" in source and "return 2" in source, (
        "check_free_models.py no longer returns all three codes"
    )
    body = SCRIPT.read_text(encoding="utf-8").split("async def _main")[-1]
    assert "return 2" in body.split("return 1")[0], (
        "the no-key path no longer exits 2, so a keyless repository would look like a "
        "confirmed-retired id"
    )


def test_the_roster_free_ids_are_what_the_check_watches() -> None:
    """Two free ids, and the script's own report names them.

    Cheap, and it catches the failure where the script checks a list nothing
    configures: `configured_free_ids` returns an empty tuple and the check passes
    vacuously while the roster pins ids nobody is watching.
    """
    out = subprocess.run(
        [sys.executable, "-m", "scripts.check_free_models"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180,
        env={k: v for k, v in os.environ.items()
             if k not in ("OPENROUTER_API_KEY", "NO_PROXY", "no_proxy")})
    assert out.returncode == 2, (
        "with no key the script must exit 2 (could not check), not %d. %s"
        % (out.returncode, out.stdout[-500:])
    )
    assert "OPENROUTER_API_KEY is not set" in out.stdout, out.stdout[-500:]
    # And the roster really does carry free ids for it to watch.
    from src.shared.llm_roster import ROSTER

    free = [r.model for r in ROSTER if r.free_tier]
    assert len(free) >= 2, (
        "the roster pins %d free ids; the staleness check watches configured_free_ids"
        % len(free)
    )
    assert all(m.endswith(":free") for m in free), free