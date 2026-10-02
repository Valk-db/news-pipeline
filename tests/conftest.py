"""Pytest configuration and shared fixtures."""

import pytest
import asyncio
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from src.schema.models import Base, SourceTier
from src.shared.config import Settings
from src.shared.config import get_settings
from src.shared import database as database_module


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def test_settings() -> Settings:
    """Test settings with in-memory database."""
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        groq_api_key="",
        cerebras_api_key="",
    )


@pytest.fixture
async def db_engine(test_settings: Settings):
    """Create async engine for testing with shared in-memory SQLite.

    Uses StaticPool so all connections reuse the same underlying connection
    (and thus the same in-memory database). This is critical for tests that
    inject the engine into database_module._engine and expect tables to
    persist across connections (e.g., the FastAPI TestClient creating new
    connections).
    """
    from sqlalchemy.pool import StaticPool
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a database session for testing."""
    async_session = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


@pytest.fixture(autouse=True)
def budget_counter(tmp_path, monkeypatch):
    """Give every test a working daily budget counter, isolated per test.

    The budget fails safe by design: with no reachable counter it refuses to spend. In
    production that is right, and in a test it would silently stop every LLM call from
    being made at all -- the provider retry and fallback tests would see zero calls. So
    each test gets its own file-backed SQLite counter, injected as the app's engine: the
    cap is real (a test can spend twice and see the second refused) and one test's
    spending never reaches the next.

    The DDL is the one from supabase/migrations/20261001000800_budget_counters.sql, minus
    the index and RLS, which SQLite has no use for.
    """
    from src.shared import budget as budget_module

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}")

    async def _create():
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE budget_counters ("
                "name TEXT NOT NULL, day DATE NOT NULL, used BIGINT NOT NULL DEFAULT 0,"
                "PRIMARY KEY (name, day))"
            )

    asyncio.run(_create())
    monkeypatch.setattr(budget_module, "_get_engine", lambda: engine)
    yield engine
    asyncio.run(engine.dispose())


# Sample article data for tests
@pytest.fixture
def sample_articles() -> list[dict]:
    return [
        {
            "url": "https://apnews.com/article/test-1",
            "title": "Test Article 1",
            "body_text": "This is the body of test article one about politics and government.",
            "source_domain": "apnews.com",
            "source_tier": SourceTier.TIER1,
            "published_at": "2024-01-15T10:00:00+00:00",
            "entities": {"PERSON": ["John Doe"], "ORG": ["AP"], "GPE": ["United States"]},
        },
        {
            "url": "https://reuters.com/article/test-2",
            "title": "Test Article 2",
            "body_text": "This is the body of test article two about politics and government.",
            "source_domain": "reuters.com",
            "source_tier": SourceTier.TIER1,
            "published_at": "2024-01-15T11:00:00+00:00",
            "entities": {"PERSON": ["Jane Smith"], "ORG": ["Reuters"], "GPE": ["United States"]},
        },
        {
            "url": "https://bbc.com/article/test-3",
            "title": "Test Article 3",
            "body_text": "Completely different article about sports and football.",
            "source_domain": "bbc.com",
            "source_tier": SourceTier.TIER1,
            "published_at": "2024-01-15T12:00:00+00:00",
            "entities": {"PERSON": ["Player One"], "ORG": ["FIFA"], "GPE": ["England"]},
        },
    ]


@pytest.fixture(autouse=True)
def _isolate_global_state():
    """Isolate settings cache and DB engine between tests.

    Records the module-level _engine and _async_session_maker from
    src.shared.database before each test, yields, then restores both
    and calls get_settings.cache_clear(). This prevents test_curation_ui.py
    from poisoning the global state that test_verification.py depends on.
    """

    # Save original globals
    orig_engine = database_module._engine
    orig_session_maker = database_module._async_session_maker

    try:
        yield
    finally:
        # Restore globals
        database_module._engine = orig_engine
        database_module._async_session_maker = orig_session_maker
        get_settings.cache_clear()