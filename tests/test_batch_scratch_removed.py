"""The batch scratch removed in this commit must not come back.

BACKLOG "Drift cleanup batch" item 2. Four paths were `git rm`'d and added to
`.gitignore`. Two separate things have to hold for that to be a fix rather than
a deletion:

1. the paths are neither on disk nor tracked, and
2. `.gitignore` still ignores them, so the next `git add -A` does not quietly
   put them back.

`.gitignore` is the only part that can rot silently - someone tidying the file
would not know these entries exist - so it is asserted here rather than trusted.
The doc pointers to the removed worker copy are asserted too, including that
the commit they cite still resolves, because a reference to history that does
not exist is a dangling reference wearing a citation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ci.gates import REPO_ROOT

# Removed path -> the commit that last tracked it. `git show <sha>:<path>` has
# to keep working, because that is now where the content lives.
REMOVED = {
    "STEALTH-REPORT.md": "4f148ee566d9dade9e5a5dab6514d7e71955a915",
    "BRIEF-canon-harden.md": "e3549a914ec7c7b6714db2e50aff303e6dbb87f4",
    "AGENT_TASKS_v38.md": "3eeaa07325d1fedbfc927e36e0795ab3453cab19",
    ".batch-refs/worker-ingest_gdelt_daily.REF.py": (
        "0d3b88276178715ad53dee3888836aafab04f53e"
    ),
}

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO_ROOT / ".git").exists(),
    reason="needs a git checkout; CI uses actions/checkout, a source export does not",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class TestRemovedPaths:
    @pytest.mark.parametrize("path", sorted(REMOVED))
    def test_not_on_disk(self, path: str) -> None:
        assert not (REPO_ROOT / path).exists(), f"{path} is back on disk"

    @pytest.mark.parametrize("path", sorted(REMOVED))
    def test_not_tracked(self, path: str) -> None:
        # `ls-files --error-unmatch` exits non-zero when the path is untracked,
        # which is the assertion: a re-added file fails here.
        proc = _git("ls-files", "--error-unmatch", "--", path)
        assert proc.returncode != 0, f"{path} is tracked again:\n{proc.stdout}"

    @pytest.mark.parametrize("path", sorted(REMOVED))
    def test_still_ignored_by_gitignore(self, path: str) -> None:
        # The entry rotting is the failure this catches: remove the .gitignore
        # line and this goes red.
        proc = _git("check-ignore", "-q", "--", path)
        assert proc.returncode == 0, f"{path} is no longer ignored by .gitignore"

    @pytest.mark.parametrize(("path", "sha"), sorted(REMOVED.items()))
    def test_still_reachable_in_history(self, path: str, sha: str) -> None:
        # History is deliberately NOT rewritten, so this has to keep resolving -
        # it is what docs/url-canonicalization-v1.md now tells a reader to run.
        proc = _git("cat-file", "-e", f"{sha}:{path}")
        assert proc.returncode == 0, f"{sha}:{path} does not resolve"

    def test_nothing_new_landed_in_batch_refs(self) -> None:
        assert not (REPO_ROOT / ".batch-refs").exists()


class TestIgnorePatternsAreNotOverBroad:
    """`AGENT_TASKS.md` is a different file and stays. Prove the patterns spare it.

    The obvious over-correction after this deletion is to gitignore
    `AGENT_TASKS*` or `*.md`, which would silently drop a real tracked doc from
    every future commit. These are the files a reviewer would assume survived.
    """

    @pytest.mark.parametrize(
        "path", ["AGENT_TASKS.md", "README.md", "DECISIONS.md", "SECURITY_AUDIT_OBSERVATIONS.md"]
    )
    def test_live_docs_are_not_ignored(self, path: str) -> None:
        assert _git("check-ignore", "-q", "--", path).returncode != 0, (
            f"{path} is now ignored by mistake"
        )

    @pytest.mark.parametrize(
        "path", ["AGENT_TASKS.md", "README.md", "DECISIONS.md", "SECURITY_AUDIT_OBSERVATIONS.md"]
    )
    def test_live_docs_are_still_tracked(self, path: str) -> None:
        assert _git("ls-files", "--error-unmatch", "--", path).returncode == 0


class TestNoDanglingReferences:
    """A removed path may only be mentioned as history, never as a live file."""

    HISTORY_MARKERS = ("git show", "removed from the tree", "was removed", "has since been deleted")

    def _tracked_text_files(self) -> list[Path]:
        proc = _git("ls-files", "-z")
        assert proc.returncode == 0
        out = []
        for name in proc.stdout.split("\0"):
            if not name:
                continue
            path = REPO_ROOT / name
            if path.suffix in {".md", ".py", ".toml", ".yml", ".yaml", ".json", ".txt"}:
                out.append(path)
        return out

    def test_mentions_are_history_references_only(self) -> None:
        offenders: list[str] = []
        for path in self._tracked_text_files():
            # .gitignore holds the rules themselves (the reason is in the comment
            # above them), and this module holds the registry of what was
            # removed. A registry is not a dangling reference. Both exclusions
            # are by construction, named here, and both would have to be
            # re-justified to widen.
            if path.name in {".gitignore"} or path.resolve() == Path(__file__).resolve():
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for number, line in enumerate(lines, start=1):
                if not any(removed in line for removed in REMOVED):
                    continue
                # Markdown wraps, so the "this is history" marker is often on an
                # adjacent line rather than the one naming the path. Read a small
                # window instead of one line, or every wrapped citation reads as a
                # dangling reference.
                window = "\n".join(lines[max(0, number - 2) : number + 2])
                if not any(marker in window for marker in self.HISTORY_MARKERS):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
        assert not offenders, (
            "these lines point at a removed file as if it still existed:\n"
            + "\n".join(offenders)
        )

    def test_the_worker_reference_doc_cites_a_real_commit(self) -> None:
        doc = (REPO_ROOT / "docs" / "url-canonicalization-v1.md").read_text(encoding="utf-8")
        cited = set(re.findall(r"`git show\s+([0-9a-f]{7,40}):", doc))
        assert cited, "the doc no longer cites a commit for the removed worker copy"
        for sha in cited:
            resolved = _git("rev-parse", "--verify", f"{sha}^{{commit}}")
            assert resolved.returncode == 0, f"cited commit {sha} does not resolve"
            # `rev-parse` answers with the full sha; requiring it to start with
            # the abbreviation is what makes the citation unambiguous.
            assert resolved.stdout.strip().startswith(sha), (
                f"cited commit {sha} resolves to a different commit: "
                f"{resolved.stdout.strip()}"
            )