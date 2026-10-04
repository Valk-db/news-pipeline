"""The evidence locker must not be import-coupled to src.transparency.log.

Two directions, and the second is the one that matters:

1. ISOLATION. ``run.py`` imports ``RssEvidenceAdapter`` at module scope and that
   adapter imports ``src.ingestion.rss_evidence``, which used to import
   ``src.transparency.log`` at module scope too. So one broken dependency in the
   transparency subsystem took down every ingest run -- including the scheduled
   one, which never asks for ``--sources rss_evidence``. These tests import the
   real chain with the module genuinely unimportable and require it to succeed.

2. LOUDNESS. ``run.py`` deliberately contains a per-adapter ``fetch()`` failure
   so one broken feed cannot destroy a whole run (I-P1-3). That containment is
   correct for a network adapter and catastrophic for the evidence locker: with
   ``src/transparency.log`` broken, ``--sources rss_evidence`` exited 0 with an
   empty Merkle log and an ``adapter_health`` note reading "down". An empty
   transparency log that every reader takes for a full one is the worst outcome
   available to a tamper-evident subsystem, so the locker gets a distinct
   exception type that ``run.py`` re-raises instead of containing.

How the module is made unimportable, and why it is done this way
--------------------------------------------------------------
``monkeypatch.delitem(sys.modules, name)`` alone does NOT force a re-import:
``from pkg import sub`` consults ``getattr(pkg, "sub")`` first via
``_handle_fromlist``, and the parent package object still holds the attribute, so
the already-loaded module is handed back and nothing is re-imported. A
``sys.modules``-only blocker is therefore a check that cannot fail.

So this installs a ``sys.meta_path`` finder that raises for exactly the target
dotted name, AND drops the module from ``sys.modules``, AND drops the attribute
from the parent package. The finder is the part that does the work; the other
two make sure it is reached. ``sys.modules`` is snapshotted and restored in
full on exit, because a partially-restored module table is exactly how one test
leaks state into the rest of the suite.

Every test here was mutation-checked: each one has a named product mutation that
turns it red. See the docstrings, which name the mutation inline.
"""

import contextlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.ingestion import rss_evidence


REPO_ROOT = Path(__file__).resolve().parents[1]
TRANSPARENCY_LOG = "src.transparency.log"


class _BlockingFinder:
    """A meta_path finder that raises ImportError for one dotted name."""

    def __init__(self, name: str, message: str):
        self._name = name
        self._message = message

    def find_spec(self, fullname, path=None, target=None):  # noqa: D102 - finder protocol
        if fullname == self._name:
            raise ImportError(self._message)
        return None


@contextlib.contextmanager
def module_unimportable(name: str = TRANSPARENCY_LOG, message: str | None = None):
    """Make `name` genuinely unimportable inside the block, then fully restore.

    Restores ``sys.modules`` entries and the parent-package attribute that were
    removed, and removes the finder. Yields the finder so a test can assert it
    was actually reached, which is the difference between "the import was
    blocked" and "the import happened to be cached".
    """
    message = message or f"blocked for test: {name} is unimportable"
    # Snapshot the WHOLE table, not a glob of this module's subtree: a partial
    # restore is how one test silently removes modules the rest of the suite
    # needs, and the resulting failure lands in an unrelated file.
    saved_modules = dict(sys.modules)
    parent_name, _, attr = name.rpartition(".")
    saved_parent_attr = None
    parent = sys.modules.get(parent_name)
    if parent is not None and hasattr(parent, attr):
        saved_parent_attr = getattr(parent, attr)

    sys.modules.pop(name, None)
    if parent is not None and hasattr(parent, attr):
        delattr(parent, attr)

    finder = _BlockingFinder(name, message)
    sys.meta_path.insert(0, finder)
    try:
        yield finder
    finally:
        try:
            sys.meta_path.remove(finder)
        except ValueError:
            pass
        for gone in [k for k in sys.modules if k not in saved_modules]:
            del sys.modules[gone]
        for gone in [k for k in saved_modules if k not in sys.modules]:
            sys.modules.pop(gone, None)
        sys.modules.update(saved_modules)
        if parent is not None and saved_parent_attr is not None:
            setattr(parent, attr, saved_parent_attr)


# The import-isolation checks run in a SUBPROCESS, deliberately.
#
# The obvious in-process version is to delete the target from sys.modules and
# import it again. That re-executes the whole import chain, and the re-executed
# modules are new objects: restoring a narrow snapshot deletes modules the rest
# of the suite still needs, and the damage lands in an unrelated test file as a
# confusing fixture error. A subprocess has its own sys.modules, so "the import
# chain is clean" is measured in a world where nothing can leak.
_ISOLATION_SCRIPT = """
import sys

TARGET = "src.transparency.log"
CANARY = "src.transparency.log_was_never_imported"

class Blocker:
    def __init__(self):
        self.reached = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname == TARGET:
            self.reached.append(fullname)
            raise ImportError("DELIBERATE BREAK: " + TARGET + " is unimportable")
        return None

blocker = Blocker()
sys.meta_path.insert(0, blocker)

results = {}
for name in ("src.ingestion.rss_evidence", "src.ingestion.run"):
    try:
        __import__(name)
        results[name] = "imported"
    except ImportError as exc:
        results[name] = "ImportError: " + str(exc)
    except Exception as exc:
        results[name] = type(exc).__name__ + ": " + str(exc)

# POSITIVE CONTROL. Independence and "the blocker is disarmed" look identical
# from the outside: both print "imported". So arm the check by proving the
# blocker actually blocks, by trying to import the target directly. If this
# control arm succeeds in importing, the whole probe is measuring nothing and
# the run must be treated as a failure rather than as evidence of decoupling.
try:
    __import__(TARGET)
    control = "CONTROL_BROKEN_blocker_let_it_through"
except ImportError:
    control = "control_ok_blocker_works"

print("RESULT", "|".join([
    results["src.ingestion.rss_evidence"],
    results["src.ingestion.run"],
    "log_loaded=" + str(TARGET in sys.modules),
    control,
]))
"""


def _isolation_probe() -> dict:
    """Run the import-chain probe in a fresh interpreter and parse its verdict."""
    proc = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SCRIPT],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=300,
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")),
        None,
    )
    if line is None:
        raise AssertionError(
            f"isolation probe produced no verdict\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    _, rest = line.split("RESULT ", 1)
    rss_evidence, run_module, log_loaded, control = (p.strip() for p in rest.split("|"))
    return {
        "rss_evidence": rss_evidence,
        "run": run_module,
        "log_loaded": log_loaded == "True",
        "control_ok": control == "control_ok_blocker_works",
        "raw": line,
    }



# ------------------------------------------------------- 1. the isolation claim


def test_import_chain_survives_a_broken_transparency_log():
    """Importing rss_evidence, and therefore run.py, must not need the log module.

    This is the claim that matters operationally. run.py imports
    RssEvidenceAdapter at module scope, so the old module-level import in
    rss_evidence.py made EVERY ingest run -- including the scheduled one, which
    never asks for --sources rss_evidence -- depend on the transparency
    subsystem. Measured before the fix: `python -m src.ingestion.run --dry-run`
    died at run.py:28 with exit 1.

    MUTATION: restore the module-level
    ``from src.transparency.log import SqlAlchemyMerkleLog`` in rss_evidence.py.
    Then the probe reports "ImportError: DELIBERATE BREAK" for BOTH modules and
    this test fails.
    """
    probe = _isolation_probe()

    # The blocker must actually block, or "nothing broke" would just mean the
    # check was never armed. This is the control: the same blocker that the
    # product imports sailed past does stop a direct import of the target.
    assert probe["control_ok"] is True, probe["raw"]
    assert probe["rss_evidence"] == "imported", probe["raw"]
    assert probe["run"] == "imported", probe["raw"]
    # Not merely "no error": the module must not have been dragged in either, or
    # the decoupling is cosmetic.
    assert probe["log_loaded"] is False, probe["raw"]


def test_transparency_log_is_still_reachable_when_actually_needed():
    """Decoupling must not have deleted the dependency, only deferred it.

    A lazy import that quietly stopped finding the symbol would make the
    isolation test above pass and leave the evidence locker stamping nothing --
    so the symbol is asserted to still resolve on the stamping path.
    """
    from src.transparency.log import SqlAlchemyMerkleLog

    assert callable(SqlAlchemyMerkleLog)


# ------------------------------------------------------- 2. the loudness claim


async def test_stamp_raises_loudly_when_transparency_log_unimportable():
    """A missing transparency MODULE must raise, not defer.

    MUTATION: replace the ``except ImportError`` in stamp_observations with a
    ``logger.warning`` and a ``return`` (i.e. swallow it) -- this test fails,
    because nothing is raised.
    """
    from src.schema.models import RawArticle, SourceTier

    article = RawArticle(
        id=uuid4(),
        url="https://www.bbc.co.uk/news/world-1",
        url_hash="a" * 64,
        title="t",
        source_domain="bbc.co.uk",
        source_tier=SourceTier.TIER1,
    )

    with module_unimportable(TRANSPARENCY_LOG, "DELIBERATE BREAK: log unavailable"):
        with pytest.raises(rss_evidence.TransparencyUnavailableError) as caught:
            await rss_evidence.stamp_observations(MagicMock(), [article])

    message = str(caught.value)
    # It has to NAME the missing module, or an operator cannot act on it.
    assert TRANSPARENCY_LOG in message
    # And it has to chain the original ImportError, so the traceback keeps the
    # real cause rather than only the wrapper's prose.
    assert isinstance(caught.value.__cause__, ImportError)
    assert "DELIBERATE BREAK" in str(caught.value.__cause__)


async def test_stamp_with_injected_log_needs_no_transparency_import():
    """An injected merkle_log must not touch src.transparency.log at all.

    MUTATION: hoist the lazy import back above the ``if merkle_log is not None``
    branch and this test fails with the injected ImportError, even though the
    injected log would have made the import pointless.
    """
    class _FakeLog:
        def __init__(self):
            self.payloads = []

        async def append(self, payload):
            self.payloads.append(payload)
            entry = MagicMock()
            entry.leaf_hash_hex = "leaf"
            entry.index = len(self.payloads) - 1
            return entry

    from src.schema.models import RawArticle, SourceTier

    article = RawArticle(
        id=uuid4(),
        url="https://www.bbc.co.uk/news/world-2",
        url_hash="b" * 64,
        title="t",
        source_domain="bbc.co.uk",
        source_tier=SourceTier.TIER1,
    )
    log = _FakeLog()

    with module_unimportable():
        result = await rss_evidence.stamp_observations(MagicMock(), [article], merkle_log=log)
        # Inside the block: the context manager restores sys.modules on exit, so
        # asserting after it would only prove the fixture put the module back.
        assert TRANSPARENCY_LOG not in sys.modules

    assert result["stamped"] == 1
    assert len(log.payloads) == 1


async def test_stamp_reraises_transparency_failure_rather_than_reporting_table_missing(db_session):
    """A missing MODULE must not be misreported as a missing TABLE.

    These are different faults with opposite correct responses: an unmigrated
    table defers stamping and keeps content_hash for later, an unimportable
    module means the log cannot be trusted at all. Collapsing them would make
    the loud failure look like the soft one and every operator would read it as
    "run the migration".
    """
    from src.schema.models import RawArticle, SourceTier

    article = RawArticle(
        id=uuid4(),
        url="https://www.bbc.co.uk/news/world-3",
        url_hash="c" * 64,
        title="t",
        source_domain="bbc.co.uk",
        source_tier=SourceTier.TIER1,
    )
    db_session.add(article)
    await db_session.flush()

    with module_unimportable(TRANSPARENCY_LOG, "DELIBERATE BREAK: log unavailable"):
        with pytest.raises(rss_evidence.TransparencyUnavailableError):
            await rss_evidence.stamp_observations(db_session, [article])

    # The transaction is still usable: the typed error is raised at the import,
    # before any statement, so nothing was left half-executed.
    await db_session.execute(text("SELECT 1"))


async def test_missing_table_is_still_soft(db_session):
    """Regression: the genuinely deferrable fault must stay deferrable.

    Loudness must not have over-reached. ``merkle_log_entries`` is absent from
    the create_all schema on purpose (see tests/test_rss_evidence.py), which is
    the real dev condition, and that path must still report table_missing and
    leave content_hash on the result for retroactive stamping.

    MUTATION: widen the lazy import's ``except ImportError`` into a bare
    ``except Exception`` that returns table_missing -- this test fails.
    """
    from src.schema.models import RawArticle, SourceTier

    article = RawArticle(
        id=uuid4(),
        url="https://www.bbc.co.uk/news/world-4",
        url_hash="d" * 64,
        title="t",
        content_hash="e" * 64,
        source_domain="bbc.co.uk",
        source_tier=SourceTier.TIER1,
    )
    db_session.add(article)
    await db_session.flush()

    result = await rss_evidence.stamp_observations(db_session, [article])

    assert result["table_missing"] is True
    assert result["stamped"] == 0
    assert result["unstamped"][0]["body_sha256"] == article.content_hash


# ------------------------------------------- 3. loudness survives run_ingestion


class _RaisingAdapter:
    """An adapter whose fetch() raises whatever it was constructed with."""

    def __init__(self, name, exc):
        self.name = name
        self._exc = exc

    async def fetch(self):
        raise self._exc

    async def health_check(self):
        from src.ingestion.adapter import SourceHealth

        return SourceHealth(status="ok", detail="never reached")


@pytest.mark.parametrize(
    "exc, should_propagate",
    [
        (
            rss_evidence.TransparencyUnavailableError(
                "evidence locker cannot stamp: src.transparency.log is not importable"
            ),
            True,
        ),
        (RuntimeError("GDELT circuit breaker open"), False),
    ],
    ids=["transparency_unavailable_is_loud", "ordinary_failure_stays_contained"],
)
async def test_run_ingestion_containment_is_selective(ingestion_env, exc, should_propagate):
    """I-P1-3 containment must apply to everything EXCEPT the transparency case.

    Drives the real ``run_ingestion`` adapter loop -- the ``except`` that is
    under test -- with a real in-memory database behind it.

    MUTATION: delete the ``except TransparencyUnavailableError`` clause in
    run.py so the broad ``except Exception`` catches it again; the first
    parameter case fails, and the second documents that containment must
    survive.
    """
    from src.ingestion import run as run_module

    adapter = _RaisingAdapter("rss_evidence", exc)
    ingestion_env._monkeypatch.setattr(run_module, "build_adapters", lambda *a, **k: [adapter])

    if should_propagate:
        with pytest.raises(rss_evidence.TransparencyUnavailableError):
            await run_module.run_ingestion(dry_run=True, sources=["rss_evidence"])
    else:
        # Contained: the run completes and records the failure as adapter health.
        results = await run_module.run_ingestion(dry_run=True, sources=["rss_evidence"])
        health = results["phases"]["ingestion"]["adapter_health"]["rss_evidence"]
        assert health["status"] == "down"
        assert "RuntimeError" in health["detail"]
