"""Guards on the ingest entry point's import surface.

The coupling this file exists to prevent:

    src/ingestion/run.py            (module level)
      -> adapters.rss_evidence_adapter
        -> src.ingestion.rss_evidence        (rss_evidence.py:85, module level)
          -> src.transparency.log            (module level)

So one broken import in the Merkle log killed *every* ingest run, including the
tiered runs that never touch the evidence locker. That is the same failure shape
as the ner hotfix in 324c1d0, one layer further out.

The fix is a lazy import inside build_adapters(), gated on --sources actually
naming rss_evidence. Two things have to hold at once and they pull opposite ways,
which is why they are asserted separately rather than as one happy path:

  1. A run that does not want the locker must not be able to die from it.
  2. A run that *does* want the locker must fail loudly when it cannot be built.

Getting (2) wrong in the permissive direction is the dangerous one: a
try/except around the import would produce a run that reports success, ingests
nothing, and appends nothing to merkle_log_entries. Every /proof permalink would
keep rendering "pending" and nothing would say why. A loud outage is
recoverable; silent loss of tamper evidence is not.

Both directions are driven by a real import failure, not a mock: a meta_path
finder raises ImportError for src.transparency.log, exactly as a syntax error or
a missing dependency in that file would, and the evidence adapter's module
entries are evicted from sys.modules *and* from their parent packages first so
the chain genuinely re-executes.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Modules that must be evicted for the block below to bite, in dependency order.
# Dropping only the adapter is not enough: rss_evidence itself holds the
# transparency import, and Python will happily hand back the already-loaded
# copy from sys.modules without re-running it.
EVIDENCE_CHAIN = (
    "src.ingestion.adapters.rss_evidence_adapter",
    "src.ingestion.rss_evidence",
    "src.transparency.log",
)

BLOCKED = "src.transparency.log"


class _Blocker:
    """A meta_path finder that fails src.transparency.log the way a real one would."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == BLOCKED or fullname.startswith(BLOCKED + "."):
            raise ImportError("DELIBERATE: src.transparency.log is unavailable")
        return None


class _Settings:
    gdelt_enabled = False


@pytest.fixture
def transparency_log_unimportable(monkeypatch):
    """Make importing src.transparency.log raise, and undo it afterwards.

    Evicting a module from sys.modules is not enough to make a `from package
    import submodule` statement re-execute it: the parent package object keeps
    the attribute it was set to on the first import, and _handle_fromlist
    returns that attribute without consulting sys.modules or meta_path at all.
    So both the sys.modules entry and the parent attribute have to go. Forget the
    second half and this fixture silently stops blocking -- which is exactly what
    happened the first time round, and it only showed up in a full-suite run
    where an earlier test had already imported the chain.
    """
    for name in EVIDENCE_CHAIN:
        parent_name, _, attr = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and getattr(parent, attr, None) is sys.modules.get(name):
            monkeypatch.delattr(parent, attr, raising=False)
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Blocker()] + list(sys.meta_path))


def test_importing_run_does_not_pull_in_the_transparency_log():
    """A fresh interpreter can import the entry point with the log unimportable.

    Run in a subprocess so the assertion is about a cold import, not about
    whatever some earlier test already left in sys.modules.
    """
    program = textwrap.dedent(
        """
        import sys

        class _Blocker:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "src.transparency.log":
                    raise ImportError("DELIBERATE: src.transparency.log is unavailable")
                return None
        sys.meta_path.insert(0, _Blocker())
        import src.ingestion.run  # noqa: F401

        leaked = sorted(
            name
            for name in sys.modules
            if name == "src.transparency.log"
            or name == "src.ingestion.rss_evidence"
            or name == "src.ingestion.adapters.rss_evidence_adapter"
        )
        print("LEAKED:" + ",".join(leaked))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "importing src.ingestion.run failed while src.transparency.log was "
        f"unimportable, so the entry point is still coupled to the Merkle log.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    line = next(
        line for line in result.stdout.splitlines() if line.startswith("LEAKED:")
    )
    assert line == "LEAKED:", (
        f"importing src.ingestion.run pulled in {line[len('LEAKED:'):]!r}. "
        "The evidence locker must stay off the entry point's import path."
    )


def test_a_run_that_does_not_want_the_locker_survives_a_broken_log(
    transparency_log_unimportable,
):
    """--sources naming an ordinary source must not touch the Merkle log."""
    from src.ingestion.run import build_adapters
    from src.ingestion.source_registry import SourceTier

    adapters = build_adapters(_Settings(), [SourceTier.TIER1], sources=["rss_tier1"])
    assert [a.name for a in adapters] == ["rss_tier1"]


def test_the_evidence_run_fails_loudly_rather_than_skipping(
    transparency_log_unimportable,
):
    """--sources rss_evidence must raise, not warn and carry on silently.

    The permissive failure here is the expensive one: a swallowed import error
    here means articles are fetched, stamped with nothing, and reported as a
    healthy run, so the log stops growing and no /proof permalink ever resolves.
    """
    from src.ingestion.run import build_adapters

    with pytest.raises(ImportError, match="DELIBERATE"):
        build_adapters(_Settings(), [], sources=["rss_evidence"])


def test_build_adapters_source_is_read_before_the_import():
    """The lazy import must sit behind the name check, not replace it.

    A bare lazy import at the top of the --sources branch would still couple
    every filtered run to the log, just a few lines later. The guard is that
    the import is only reached when 'rss_evidence' is among the wanted names.
    """
    import inspect

    from src.ingestion.run import build_adapters

    source = inspect.getsource(build_adapters)
    import_line = next(
        line
        for line in source.splitlines()
        if "rss_evidence_adapter import RssEvidenceAdapter" in line
    )
    guard = next(
        line
        for line in source.splitlines()
        if "rss_evidence" in line and line.strip().startswith("if ")
    )
    assert source.index(guard) < source.index(import_line), (
        "the lazy import runs before the 'rss_evidence' name check, so every "
        "--sources run is coupled to the Merkle log again"
    )
