"""The secret scan has to be free, pinned, and narrow.

Three things are pinned here, each of which has a specific way of rotting:

1. The workflow stays credential-free and version-pinned. A scanner that needs
   an account or a floating `latest` tag is not free and not reproducible, and
   the obvious future edit - "just use the vendor's action" - reintroduces a
   licence requirement that this batch was told to avoid.
2. `.gitleaks.toml` keeps the default ruleset. A hand-written pattern list is the
   "pattern scan was not exhaustive" thing this scan exists to replace.
3. The allowlist stays as narrow as it can be while still letting CI pass today.
   Every entry names one file by name, no entry is a glob, nothing under
   `tests/` is allowlisted, every entry names a file that exists, and every
   tracked file the allowlist was written for is still covered.

These are text assertions on the workflow rather than a YAML parse, for the same
reason as in `tests/test_ci_gates.py`: PyYAML only reaches uv.lock through the
`enrichment` extra, so a committed guard that parsed these files would be skipped
in CI's `--extra dev --extra pipeline` environment and would silently never run.
A malformed workflow is caught by GitHub itself, which refuses to load it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from ci.gates import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "secret-scan.yml"
CONFIG = REPO_ROOT / ".gitleaks.toml"
RESULTS_DIR = "eval/data/results"


def _tracked() -> set[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return {name for name in proc.stdout.split("\0") if name}


def _allowlist_paths() -> list[str]:
    """The `paths` entries of every [[allowlists]] block, as regex bodies.

    Anchors are stripped so callers can make statements about the path itself
    ("it ends in .json", "it names exactly one file") instead of about the regex
    syntax wrapped around it.
    """
    paths: list[str] = []
    in_paths = False
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("paths"):
            in_paths = True
            continue
        if stripped.startswith("["):
            in_paths = False
            continue
        if in_paths and stripped.startswith("'"):
            body = stripped.strip("',").replace("\\", "")
            paths.append(body.removeprefix("^").removesuffix("$"))
    return paths


def _to_regex(body: str) -> re.Pattern[str]:
    return re.compile("^" + body + "$")




@pytest.fixture(scope="module")
def workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.mark.skipif(
    shutil.which("git") is None or not (REPO_ROOT / ".git").exists(),
    reason="needs a git checkout; CI uses actions/checkout, a source export does not",
)
class TestWorkflow:
    def test_runs_on_every_push_not_only_main(self, workflow: str) -> None:
        # ci.yml triggers on `push: branches: [main]`, so a key committed on a
        # feature branch is not scanned until merge. This workflow must not
        # repeat that: a bare `push:` is every branch.
        triggers = re.search(r"^on:\n((?:  .*\n|\n)+)", workflow, re.MULTILINE)
        assert triggers, "no trigger block found"
        assert re.search(r"^  push:\s*$", triggers.group(1), re.MULTILINE), (
            "the secret scan must trigger on a bare `push:` (every branch)"
        )
        assert "branches:" not in triggers.group(1)

    def test_no_credentials_or_secrets_are_referenced(self, workflow: str) -> None:
        # Free and account-free is a requirement, not a nicety: the vendor's
        # action needs a licence for org-owned repos.
        assert "secrets." not in workflow
        assert "GITLEAKS_LICENSE" not in workflow

    def test_version_is_pinned(self, workflow: str) -> None:
        versions = re.findall(r'GITLEAKS_VERSION:\s*"([^"]+)"', workflow)
        assert len(versions) == 1, "pin exactly one gitleaks version"
        assert re.fullmatch(r"\d+\.\d+\.\d+", versions[0]), versions[0]

    def test_download_is_checksum_verified(self, workflow: str) -> None:
        # An unpinned download is a supply-chain hole; an unpinned *checksum* is
        # the same hole with extra steps.
        digests = re.findall(r'GITLEAKS_SHA256:\s*"([^"]+)"', workflow)
        assert len(digests) == 1, "pin exactly one sha256"
        assert re.fullmatch(r"[0-9a-f]{64}", digests[0]), digests[0]
        assert "sha256sum -c" in workflow

    def test_scans_history_not_just_the_tree(self, workflow: str) -> None:
        assert "fetch-depth: 0" in workflow
        assert "./gitleaks git" in workflow
        assert "./gitleaks dir" in workflow

    def test_uses_a_verified_action_reference(self, workflow: str) -> None:
        # actions/checkout@v7 resolved on 2026-10-03 (latest tag v7.0.1); an
        # action pinned to a ref that does not exist is the pgvector failure.
        assert "uses: actions/checkout@v7" in workflow
        assert "gitleaks/gitleaks-action" not in workflow


@pytest.mark.skipif(
    shutil.which("git") is None or not (REPO_ROOT / ".git").exists(),
    reason="needs a git checkout; CI uses actions/checkout, a source export does not",
)
class TestAllowlistStaysNarrow:
    def test_default_ruleset_is_kept(self) -> None:
        # The default ~180 rules are the teeth: they catch Groq `gsk_` keys,
        # `ghp_` PATs and Supabase service_role JWTs without this repo naming any
        # of them. Replacing the ruleset with hand-written patterns is a
        # regression dressed as tidying.
        assert "useDefault = true" in CONFIG.read_text(encoding="utf-8")

    def test_there_is_exactly_one_allowlist(self) -> None:
        text = CONFIG.read_text(encoding="utf-8")
        assert text.count("[[allowlists]]") == 1
        # Fingerprint files (.gitleaksignore) are per-commit and rot silently.
        assert not (REPO_ROOT / ".gitleaksignore").exists()

    def test_every_entry_names_one_file_and_no_globs(self) -> None:
        paths = _allowlist_paths()
        assert paths, "the allowlist is empty; it is what makes CI pass today"
        for pattern in paths:
            assert not re.search(r"[*?\[]", pattern), (
                f"{pattern} is a glob: a glob would also silence files added later"
            )
            assert pattern.endswith(".json"), pattern
            # Exactly the depth of eval/data/results/, no deeper: a nested path
            # would be an entry nobody reviewed.
            assert pattern.count("/") == RESULTS_DIR.count("/") + 1, pattern

    def test_nothing_under_tests_is_allowlisted(self) -> None:
        # The brief names this one explicitly. The placeholders in tests/ are
        # cheap to prove are placeholders; allowlisting the directory would throw
        # away the scan exactly where fixtures are written.
        for pattern in _allowlist_paths():
            assert "test" not in pattern.lower(), pattern

    def test_no_entry_names_a_file_that_does_not_exist(self) -> None:
        # A dangling allowlist entry is the same defect as a dangling exclusion:
        # it looks like it is doing work and is doing nothing, and the next file
        # to land in that directory inherits a rule nobody reviewed.
        tracked = _tracked()
        for pattern in _allowlist_paths():
            matches = [name for name in tracked if _to_regex(pattern).match(name)]
            assert len(matches) == 1, f"{pattern} matches {matches}"

    def test_every_manifest_the_allowlist_exists_for_is_still_covered(self) -> None:
        # The other direction: a new tracked file under eval/data/results/ that
        # the allowlist does not name will fail CI (it has the same `key` field).
        # Failing is correct - the fix is a decision, not a wildcard.
        allowlisted = {name for name in _tracked() if name.startswith(f"{RESULTS_DIR}/")}
        covered = {
            name
            for name in allowlisted
            if any(_to_regex(pattern).match(name) for pattern in _allowlist_paths())
        }
        assert covered == allowlisted, (
            f"not covered by the allowlist: {sorted(allowlisted - covered)}"
        )

    def test_only_eval_manifests_are_allowlisted(self) -> None:
        for pattern in _allowlist_paths():
            assert pattern.startswith(f"{RESULTS_DIR}/"), pattern


class TestEvalManifestsAreTheReasonStated:
    """The allowlist exists because of one field name in these files.

    If the manifests ever stop carrying a 64-char hex value in a field called
    `key`, the allowlist is dead weight and should be deleted rather than left
    behind silently. This asserts the shape it is written against still exists.
    """

    def test_manifests_still_hold_hex_values_in_a_field_named_key(self) -> None:
        seen_fields: set[str] = set()
        hex_fields: set[str] = set()
        for path in sorted((REPO_ROOT / RESULTS_DIR).glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))

            def walk(node: object) -> None:
                if isinstance(node, dict):
                    for key, value in node.items():
                        if isinstance(value, str):
                            seen_fields.add(key)
                            if re.fullmatch(r"[0-9a-f]{64}", value):
                                hex_fields.add(key)
                        walk(value)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(payload)
        assert "key" in seen_fields, "the field the allowlist is written against is gone"
        assert "key" in hex_fields, (
            f"`key` no longer holds a 64-char hex value; fields that do: {hex_fields}"
        )