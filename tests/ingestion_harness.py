"""A harness that makes ``run_ingestion()`` testable at both of its seams.

Two independent facts about ``src/ingestion/run.py`` make the naive patching
these tests used impossible. Both were found by running them, not by reading
them, and both are silent -- the test does not error, it just asserts on
something other than what it was written to assert on.

**1. ``run_ingestion`` does not call ``ingest_rss_feeds`` / ``ingest_gdelt`` /
``ingest_reddit``.** It calls ``build_adapters(settings, tiers, sources)`` and
then ``await adapter.fetch()`` for each adapter (run.py:337, run.py:355).
Patching the old module-level functions patched nothing at all, so those tests
went to the network, ingested ~900 real articles, and asserted on the result.
The GDELT-toggle tests were worse: they asserted on a mock that is never
called, so ``assert_called_once()`` was asserting that a dead name was called
by nobody.

**2. ``get_session`` is bound by NAME into five modules** via
``from src.shared.database import get_session``: ``run.py``, ``gdelt.py``, and
the rss / reddit / rss_evidence adapters. Patching the attribute on the source
module does not rebind those names, so every adapter still opened a real
session and raised ``RuntimeError: Database not configured``. ``run_ingestion``
caught that per adapter, logged "continuing with remaining adapters", and
carried on -- which is why the dedup tests were measuring the debris of three
adapters exploding rather than the dedup rule.

This module fixes both, and deliberately does it the *real* way: the adapters
are built by the real ``build_adapters`` (so the GDELT enable/disable logic
under test is the production logic, not a reimplementation), the DB seam is a
real in-memory SQLite database (so the dedup SQL and the rows it produces are
the real ones), and only the two things that would touch the network -- each
adapter's ``fetch()`` -- are replaced with supplied stubs.
"""

from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.ingestion.adapter import SourceHealth
from src.ingestion.source_registry import SourceTier
from src.schema.models import RawArticle, StatusLog
from src.shared import database as database_module
from src.utils.trafilatura_extract import compute_content_hash, compute_url_hash


def bind_get_session_everywhere(monkeypatch, factory) -> list[str]:
    """Point every ``get_session`` binding in the process at ``factory``.

    Patches the source module's attribute *and* every already-imported module
    that bound the same function object into its own namespace. Discovery is
    by identity against the real ``src.shared.database.get_session``, so this
    cannot silently miss a new adapter: a module that does
    ``from src.shared.database import get_session`` is holding that exact
    object and will be found.

    Only already-imported modules are considered, which means no import side
    effects are triggered. ``run.py`` imports every adapter at module scope, so
    by the time any of these tests run, all five bindings exist.

    Returns the module names patched, so a test can assert the seam covered
    what it needed to cover rather than trusting it did.
    """
    real = database_module.get_session
    patched = ["src.shared.database"]
    monkeypatch.setattr(database_module, "get_session", factory)

    for name, module in list(sys.modules.items()):
        if not name.startswith("src.") or module is None:
            continue
        if getattr(module, "get_session", None) is real:
            monkeypatch.setattr(module, "get_session", factory)
            patched.append(name)

    return sorted(patched)


def make_article(
    url: str,
    body: str,
    domain: str,
    tier: SourceTier = SourceTier.TIER1,
    title: str = "Test article",
) -> RawArticle:
    """A real ``RawArticle`` with both hashes computed the way production does."""
    return RawArticle(
        id=uuid.uuid4(),
        url=url,
        url_hash=compute_url_hash(url),
        title=title,
        body_text=body,
        source_domain=domain,
        source_tier=tier,
        content_hash=compute_content_hash(body),
    )


@dataclass
class IngestionRun:
    """What the harness observed about one ``run_ingestion`` call."""

    constructed: list[str] = field(default_factory=list)
    """Adapter names the real ``build_adapters`` selected."""

    fetched: list[str] = field(default_factory=list)
    """Adapter names whose ``fetch()`` actually ran, in call order."""

    fetched_articles: list[RawArticle] = field(default_factory=list)
    """Every article handed back by every stubbed ``fetch()``, in adapter order."""

    patched_modules: list[str] = field(default_factory=list)
    """Modules whose ``get_session`` binding the harness replaced."""


def stub_adapters(
    monkeypatch,
    run_module,
    articles_by_adapter: dict[str, list[RawArticle]],
    health_by_adapter: dict[str, SourceHealth] | None = None,
) -> IngestionRun:
    """Run the REAL adapter selection, with only ``fetch()`` replaced.

    ``build_adapters`` is wrapped, not replaced: the real function decides which
    adapters exist, so ``settings.gdelt_enabled`` is exercised through the
    production branch. The wrapper then swaps each selected adapter's ``fetch``
    for a stub returning the supplied articles and records the call.

    Adapters selected by the real logic but absent from ``articles_by_adapter``
    fetch nothing, so a test that only cares about one adapter stays hermetic
    without having to enumerate the others.
    """
    health_by_adapter = health_by_adapter or {}
    run = IngestionRun()
    real_build_adapters = run_module.build_adapters

    def _stub_fetch(name: str):
        async def fetch() -> list[RawArticle]:
            run.fetched.append(name)
            articles = list(articles_by_adapter.get(name, []))
            run.fetched_articles.extend(articles)
            return articles

        return fetch

    def _stub_health(name: str):
        async def health_check() -> SourceHealth:
            if name in health_by_adapter:
                return health_by_adapter[name]
            return SourceHealth(status="ok", detail="stubbed", succeeded=[name])

        return health_check

    def build_adapters(settings, tiers, sources):
        adapters = real_build_adapters(settings, tiers, sources)
        run.constructed = [a.name for a in adapters]
        for adapter in adapters:
            adapter.fetch = _stub_fetch(adapter.name)
            adapter.health_check = _stub_health(adapter.name)
        return adapters

    monkeypatch.setattr(run_module, "build_adapters", build_adapters)
    return run


def silence_post_ingest_phases(monkeypatch, run_module) -> None:
    """Stub the phases after dedup so a test can assert on ingestion alone.

    Dedup is phase 1. Reporting-unit building, story grouping and the dynamic
    gate are separate concerns with their own suites; leaving them live would
    make a dedup test fail for a dedup-unrelated reason.
    """
    async def _units(session):
        return 0

    async def _stories(session):
        return []

    async def _gate(session, story_ids=None):
        return {"queued": 0, "blocked": 0}

    monkeypatch.setattr(run_module, "build_reporting_units", _units)
    monkeypatch.setattr(run_module, "build_stories", _stories)
    monkeypatch.setattr(run_module, "apply_dynamic_gate", _gate)


def silence_translation(monkeypatch) -> None:
    """Stop phase 1.5 from calling a translation backend.

    ``run_ingestion`` imports ``translate_articles`` from inside the function
    body, so the module attribute is the only seam.
    """
    from src.enrichment import translation

    def _translate(articles):
        return {
            "backend": "stubbed",
            "total": len(articles),
            "translated": 0,
            "english": len(articles),
            "failed": 0,
        }

    monkeypatch.setattr(translation, "translate_articles", _translate)


def settings_for(**overrides):
    """A real ``Settings`` with a real boolean ``gdelt_enabled``.

    A ``MagicMock`` settings object makes ``settings.gdelt_enabled`` truthy for
    any value including ``False``-looking ones, which is precisely the class of
    bug the GDELT toggle tests were supposed to catch. ``build_adapters``
    branches on this value, so it has to be a genuine bool.
    """
    from src.shared.config import Settings

    base = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "groq_api_key": "",
        "cerebras_api_key": "",
    }
    base.update(overrides)
    return Settings(**base)


def session_factory_for(engine):
    """An async-context-manager ``get_session`` over ``engine``.

    Mirrors the real ``database.get_session`` contract: a context manager
    yielding a session, committing on clean exit, rolling back on error, and
    always closing. Anything that uses the seam as a real session must work.
    """
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _get_session():
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    return _get_session


class IngestionEnv:
    """One test's world: a real database and real adapter selection.

    ``seed`` writes rows into ``raw_articles`` the way a previous run would
    have, so the dedup path under test compares against real stored rows rather
    than a mock's idea of them. ``fetch`` declares what the adapters return and
    re-stubs them, so each test states its own batch in its own body.
    """

    def __init__(self, engine, monkeypatch, settings_kwargs=None):
        from src.ingestion import run as run_module

        self.engine = engine
        self._monkeypatch = monkeypatch
        self._module = run_module
        self.run = IngestionRun()
        self.patched_modules: list[str] = []

        self.patched_modules = bind_get_session_everywhere(
            monkeypatch, session_factory_for(engine)
        )
        self._settings_kwargs = settings_kwargs or {}
        monkeypatch.setattr(
            run_module, "get_settings", lambda: settings_for(**self._settings_kwargs)
        )
        silence_post_ingest_phases(monkeypatch, run_module)
        silence_translation(monkeypatch)

        # The seam is only trustworthy if it covered the modules that actually
        # open sessions. Asserting it here means a new module that binds
        # get_session by name fails loudly at setup, not silently in a test.
        assert "src.ingestion.run" in self.patched_modules
        assert "src.shared.database" in self.patched_modules

    def fetch(self, articles_by_adapter, health_by_adapter=None) -> IngestionRun:
        """Declare each adapter's return value; returns the run recorder.

        The real ``build_adapters`` still decides which adapters exist, so
        ``settings.gdelt_enabled`` is exercised through the production branch.
        """
        self.run = stub_adapters(
            self._monkeypatch,
            self._module,
            articles_by_adapter,
            health_by_adapter,
        )
        self.run.patched_modules = self.patched_modules
        return self.run

    async def seed(self, *articles: RawArticle) -> None:
        """Insert articles directly, standing in for an earlier ingest run."""
        maker = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        async with maker() as session:
            session.add_all(list(articles))
            await session.commit()

    async def ingest(self, dry_run: bool = False, **kwargs) -> dict:
        return await self._module.run_ingestion(dry_run=dry_run, **kwargs)

    async def rows(self) -> list[tuple]:
        """(url, url_hash, content_hash, source_domain) for every stored row."""
        return await stored_rows(self.engine)

    async def statuses(self) -> list[tuple]:
        """(phase, status) for every row log_status wrote, in write order."""
        return await status_log_rows(self.engine)


async def stored_rows(engine) -> list[tuple]:
    """Every row in ``raw_articles``, as (url, url_hash, content_hash, domain)."""
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        result = await session.execute(
            select(
                RawArticle.url,
                RawArticle.url_hash,
                RawArticle.content_hash,
                RawArticle.source_domain,
            )
        )
        return sorted(result.all())


async def status_log_rows(engine) -> list[tuple]:
    """Every (phase, status) row written by ``log_status``, in write order."""
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        result = await session.execute(
            select(StatusLog.phase, StatusLog.status).order_by(StatusLog.id)
        )
        return result.all()
