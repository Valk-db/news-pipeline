"""Daily GDELT ingestion worker.

This file is a thin scheduler shim. All pipeline logic lives in
src/ingestion/run.py: adapter selection, dedup, checkpoints via the database
url_hash and content_hash filters, per stage statistics through
src/utils/ingest_stats.py, and dead letter handling by leaving failed
extractions counted in STATS rather than persisting partial rows. Do not
reimplement any of that here.

What this script owns, and nothing else:
  1. Run run.py with --env dev as a subprocess and propagate its exit code.
  2. Read the per stage item counts run.py prints into
     GITHUB_STEP_SUMMARY and fail loudly if a stage produced zero items.
  3. Ping a healthchecks.io dead man's switch after a successful run.

The dead man's switch uses HEALTHCHECK_UUID from the environment. When it is
unset the ping is skipped with a debug log, so a local run does not need it.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Dict, List, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ingest_gdelt_daily")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_MODULE = "src.ingestion.run"

HEALTHCHECK_URL = "https://hc-ping.com/{uuid}"
HEALTHCHECK_TIMEOUT_SECONDS = 15

# Stages that must yield at least one item. A zero here means the stage ran and
# found nothing, which is the failure this worker exists to catch.
REQUIRED_STAGES = (
    "ingestion.total_fetched",
    "ingestion.total_new",
    "reporting_units.created",
    "gate.queued",
)

# The pipeline prints a JSON document; pull phase results out of it.
JSON_START_RE = re.compile(r"^\{$", re.MULTILINE)


def run_pipeline(env: str, sources: str = "", tiers: str = "", dry_run: bool = False) -> Tuple[int, str]:
    """Invoke run.py in a subprocess. Returns (returncode, combined output)."""
    cmd = [sys.executable, "-m", RUN_MODULE, "--env", env]
    if sources:
        cmd += ["--sources", sources]
    if tiers:
        cmd += ["--tiers", tiers]
    if dry_run:
        cmd.append("--dry-run")

    logger.info("Running: %s (cwd=%s)", " ".join(cmd), REPO_ROOT)
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output


def extract_results(output: str) -> Dict:
    """Pull the pipeline results JSON out of the combined output.

    run.py prints a human log first and the JSON document last, so we take the
    last top level JSON object in the output. Returns {} when there is none.
    """
    start = output.rfind("\n{\n")
    if start == -1:
        start = output.find("{")
        if start == -1:
            return {}
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(output)):
        ch = output[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(output[start:i + 1])
                except ValueError:
                    return {}
    return {}


def stage_counts(results: Dict) -> Dict[str, int]:
    """Flatten phase results into the per stage item counts we check."""
    counts: Dict[str, int] = {}
    if not results:
        return counts
    for phase, payload in (results.get("phases") or {}).items():
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            counts[f"{phase}.{key}"] = int(value)
    return counts


def check_stages(output: str, required: Tuple[str, ...] = REQUIRED_STAGES) -> List[str]:
    """Return a list of stage names that produced zero items.

    An empty list means every required stage yielded something. When the
    results JSON is missing entirely we report a single marker rather than
    claiming success, because we cannot prove the stages ran.
    """
    results = extract_results(output)
    if not results:
        return ["<missing results>"]

    counts = stage_counts(results)
    empty: List[str] = []
    for stage in required:
        value = counts.get(stage)
        if value is None:
            empty.append(f"{stage} (stage did not report)")
        elif value == 0:
            empty.append(stage)
    return empty


def ping_healthcheck(uuid: str, timeout: int = HEALTHCHECK_TIMEOUT_SECONDS) -> bool:
    """Ping the healthchecks.io check. True when the ping was accepted."""
    url = HEALTHCHECK_URL.format(uuid=uuid)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                logger.error("Healthcheck ping returned HTTP %s", resp.status)
            return ok
    except (urllib.error.URLError, OSError) as e:
        logger.error("Healthcheck ping failed: %s", e)
        return False


def send_dead_man_signal() -> None:
    """Ping the dead man's switch, skipping quietly when unconfigured."""
    uuid = os.getenv("HEALTHCHECK_UUID", "").strip()
    if not uuid:
        logger.debug("HEALTHCHECK_UUID not set, skipping dead man's switch ping")
        return
    if ping_healthcheck(uuid):
        logger.info("Dead man's switch pinged")
    else:
        # The pipeline itself succeeded, so this is a warning, not a failure.
        logger.warning("Dead man's switch ping did not succeed")


def append_step_summary(text: str) -> None:
    """Append a summary block to GITHUB_STEP_SUMMARY when CI provides it."""
    path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(text)
    except OSError as e:
        logger.warning("Could not write GITHUB_STEP_SUMMARY: %s", e)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/ingest_gdelt_daily.py",
        description="Scheduler shim around src/ingestion/run.py. All logic lives in run.py.",
    )
    parser.add_argument(
        "--env",
        default="dev",
        help="Config profile passed through to run.py. Default dev.",
    )
    parser.add_argument(
        "--sources",
        default="",
        help="Comma separated sources passed through to run.py --sources.",
    )
    parser.add_argument(
        "--tiers",
        default="",
        help="Comma separated tiers passed through to run.py --tiers.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass --dry-run through to run.py.",
    )
    args = parser.parse_args()

    returncode, output = run_pipeline(args.env, args.sources, args.tiers, args.dry_run)
    print(output, end="" if output.endswith("\n") else "\n")

    if returncode != 0:
        logger.error("run.py exited %d, not pinging dead man's switch", returncode)
        append_step_summary("\n## GDELT daily ingest\n\nrun.py exited %d\n" % returncode)
        return returncode

    empty_stages = check_stages(output)
    if empty_stages:
        for stage in empty_stages:
            logger.error("Stage produced zero items: %s", stage)
            print(f"::error title=Zero items in stage::{stage}")
        append_step_summary(
            "\n## GDELT daily ingest\n\nFAILED, zero items in: %s\n" % ", ".join(empty_stages)
        )
        return 1

    send_dead_man_signal()
    append_step_summary("\n## GDELT daily ingest\n\nAll stages reported items.\n")
    logger.info("Ingestion run completed with items in every required stage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
