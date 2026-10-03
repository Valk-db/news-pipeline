"""Guards on the ingestion test seam itself.

The defect these guard against is not hypothetical and is not visible from any
assertion in a test that uses the seam. It has one shape: **a module acquires a
database session through a path the ingestion test harness does not cover.**

When that happened (three adapters, on every ``run_ingestion`` test, for months)
the failure was silent in the worst way. ``get_session`` is bound by name into
each importing module, so patching ``src.shared.database.get_session`` -- the
obvious fix, and the one four separate batches each assumed was needed -- does
nothing to those bindings. Every adapter opened a real session, raised
``RuntimeError: Database not configured``, and ``run_ingestion`` caught it per
adapter, logged "continuing with remaining adapters", and carried on. The tests
did not fail loudly. They asserted on a number derived from debris.

So these tests check the seam rather than the product: that the names the
harness rebinds are the names the code actually calls, and that the dedup tests
keep asserting on rows rather than drifting back to counts.
"""

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import src.ingestion
import src.shared.database as database_module
from tests.ingestion_harness import bind_get_session_everywhere

ADAPTERS_PKG = "src.ingestion.adapters"


def adapter_module_names() -> list[str]:
    """Every module in the adapters package, imported.

    Imported rather than globbed so that ``sys.modules`` contains them and the
    identity-based discovery in ``bind_get_session_everywhere`` can see any
    ``get_session`` they bind. run.py already imports all of them at module
    scope, so this adds no new import side effects in practice.
    """
    package = importlib.import_module(ADAPTERS_PKG)
    names = [ADAPTERS_PKG]
    for info in pkgutil.iter_modules(package.__path__):
        names.append(f"{ADAPTERS_PKG}.{info.name}")
    for name in names:
        importlib.import_module(name)
    return sorted(names)


def modules_binding_get_session() -> list[str]:
    """Every already-imported module holding the real ``get_session`` object."""
    import sys

    real = database_module.get_session
    return sorted(
        name
        for name, module in list(sys.modules.items())
        if name.startswith("src.")
        and module is not None
        and getattr(module, "get_session", None) is real
    )


class TestIngestionSeamCoverage:
    """The seam must cover every name the ingestion code actually calls."""

    def test_every_adapter_module_imports(self):
        """Discovery sees the whole adapters package, not a hardcoded list."""
        names = adapter_module_names()
        assert ADAPTERS_PKG in names
        # Sanity: the package is not empty and discovery is not degenerate.
        assert len(names) >= 5, names

    def test_harness_covers_every_get_session_binding(self, monkeypatch):
        """bind_get_session_everywhere finds all of them, adapters included.

        This is the test that would have caught the original defect at the
        moment someone added the first adapter. If a new module does
        ``from src.shared.database import get_session`` and run.py imports it,
        the identity scan finds it because it is holding that exact object.
        """
        import sys

        adapter_module_names()
        expected = set(modules_binding_get_session())
        assert expected, "discovery found nothing, which means it is broken"

        real = database_module.get_session
        patched = bind_get_session_everywhere(monkeypatch, object())

        assert set(patched) == expected | {"src.shared.database"}
        for name in expected:
            assert sys.modules[name].get_session is not real, name

    def test_every_adapter_opening_a_session_is_covered(self, monkeypatch):
        """Every adapter that acquires a session is in the seam's patch set.

        Stated as a property of the adapters rather than of the harness, so a
        new adapter that opens its own session cannot slip in unnoticed. An
        adapter that legitimately never touches the database is fine and is
        reported in the skip message.
        """
        names = adapter_module_names()
        real = database_module.get_session
        needs_seam = []
        for name in names:
            module = importlib.import_module(name)
            if getattr(module, "get_session", None) is real:
                needs_seam.append(name)

        patched = bind_get_session_everywhere(monkeypatch, object())
        for name in needs_seam:
            assert name in patched, (
                f"{name} acquires a DB session but is not in the seam's patch "
                f"set; the ingestion tests would let it hit the real database"
            )

    def test_no_adapter_reaches_database_module_by_attribute(self):
        """Adapters must not call ``database.get_session()`` through the module.

        Two ways to open a session exist in this codebase and only one of them
        is rebindable by ``bind_get_session_everywhere``:

        * ``from src.shared.database import get_session`` then ``get_session()``
          -- the harness rebinds the module-local name, so it is covered;
        * ``from src.shared import database`` then ``database.get_session()``
          -- this looks up the attribute at CALL time on the module object, and
          rebinding the module's attribute does cover it, but only for the
          duration of the patch.

        Reading the source rather than the runtime is deliberate: an adapter
        that is not imported by run.py would not be in ``sys.modules`` at all,
        and an unimported adapter is exactly what this guard is for. This
        asserts the seam's assumption holds in the source, not just in the
        modules that happen to be loaded.
        """
        offenders: list[str] = []
        pkg_dir = Path(src.ingestion.__file__).parent / "adapters"
        for path in sorted(pkg_dir.glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                # database.get_session() reached by attribute chain.
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get_session"
                    and isinstance(func.value, ast.Name)
                    and func.value.id in {"database", "database_module", "db"}
                ):
                    offenders.append(f"{path.name}:{node.lineno}")

        assert not offenders, (
            "these adapters call get_session() through a module attribute; "
            f"the ingestion seam must cover that form too: {offenders}"
        )

    def test_adapters_do_not_build_their_own_engine(self):
        """No adapter constructs its own engine, so the seam is the only path.

        An adapter that called ``create_async_engine()`` itself would open a
        second, unpatched connection to whatever DATABASE_URL happened to be
        set, and the ingestion tests would quietly read and write the real
        database while appearing to be hermetic.
        """
        offenders: list[str] = []
        pkg_dir = Path(src.ingestion.__file__).parent / "adapters"
        for path in sorted(pkg_dir.glob("*.py")):
            if "create_async_engine" in path.read_text():
                offenders.append(path.name)
        assert not offenders, (
            f"adapters constructing their own engine bypass the test seam: {offenders}"
        )


class TestDedupTestsAssertOnArtifacts:
    """The dedup tests must keep asserting on rows, not drift back to counts.

    A weaker version of these tests is a plausible future edit: someone
    "simplifies" an assertion from "this url survived" to "total_new == 1",
    every test still passes, and the coverage of the actual dedup rule is gone
    with nothing red to show for it. These checks read the test source and fail
    if the artifact assertions go away.
    """

    DEDUP_TEST_FILE = "test_content_hash.py"
    ARTIFACT_MARKERS = (
        "ingestion_env.rows()",
        "compute_content_hash(body)",
        "compute_url_hash(",
        "statuses()",
    )

    @pytest.fixture
    def dedup_source(self) -> str:
        return (Path(__file__).parent / self.DEDUP_TEST_FILE).read_text()

    def test_dedup_tests_read_rows_back(self, dedup_source):
        """The dedup class still asserts on rows read from the database."""
        start = dedup_source.index("class TestRunIngestionDedup:")
        end = dedup_source.index("class TestIngestionSourcesHaveContentHash:")
        body = dedup_source[start:end]
        assert "ingestion_env.rows()" in body, (
            "the dedup tests no longer read raw_articles back; they are "
            "asserting on counters only, which a crashed adapter can fake"
        )

    def test_dedup_tests_do_not_patch_get_session_by_hand(self, dedup_source):
        """The dedup tests use the shared seam, not a private re-patch.

        A hand-rolled ``patch("src.ingestion.run.get_session")`` is the exact
        construct that let three adapters escape the seam in the first place.
        """
        start = dedup_source.index("class TestRunIngestionDedup:")
        end = dedup_source.index("class TestIngestionSourcesHaveContentHash:")
        body = dedup_source[start:end]
        assert 'patch("src.ingestion.run.get_session"' not in body
        assert "src.ingestion.run.get_session" not in body

    def test_dedup_tests_assert_on_every_ingested_row(self, dedup_source):
        """Each dedup test that inserts articles also reads the table back.

        Counted rather than asserted by name, so adding a test that checks a
        different property does not require editing this guard.
        """
        start = dedup_source.index("class TestRunIngestionDedup:")
        end = dedup_source.index("class TestIngestionSourcesHaveContentHash:")
        body = dedup_source[start:end]

        test_methods = [
            chunk for chunk in body.split("    async def ")[1:]
        ]
        assert len(test_methods) >= 5, "expected the dedup tests to still be here"

        for method in test_methods:
            name = method.split("(")[0]
            if name.startswith("test_"):
                assert "ingestion_env.rows()" in method, (
                    f"{name} does not read raw_articles back, so it cannot "
                    f"distinguish 'the right rows survived' from 'the right "
                    f"number of rows survived'"
                )

    def test_harness_is_used_by_both_dedup_and_toggle_tests(self, dedup_source):
        """The toggle tests must not reintroduce a private harness either."""
        toggle_source = (Path(__file__).parent / "test_gdelt_toggle.py").read_text()
        for source, label in ((dedup_source, "content_hash"), (toggle_source, "gdelt_toggle")):
            assert "ingestion_env" in source, label
            assert 'patch("src.ingestion.run.get_session"' not in source, label
