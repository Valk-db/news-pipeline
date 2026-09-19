from contextlib import asynccontextmanager
from typing import AsyncGenerator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from src.shared.config import get_settings


_engine = None
_async_session_maker = None


def _get_engine():
    """Lazily create engine for serverless environments. Returns None if DB not configured."""
    global _engine
    if _engine is None:
        settings = get_settings()
        if not settings.has_database:
            return None
        _engine = create_async_engine(
            settings.database_url,
            poolclass=NullPool,  # Supabase/Neon work better without pooling
            connect_args={"statement_cache_size": 0},  # Disable prepared statements for Supabase transaction pooler
            echo=False,
        )
    return _engine


def _get_session_maker():
    """Lazily create session maker for serverless environments. Returns None if DB not configured."""
    global _async_session_maker
    engine = _get_engine()
    if engine is None:
        return None
    if _async_session_maker is None:
        _async_session_maker = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
    return _async_session_maker


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    session_maker = _get_session_maker()
    if session_maker is None:
        raise RuntimeError("Database not configured. Set DATABASE_URL environment variable.")
    async with session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Create tables and enable pgvector extension (PostgreSQL only)."""
    from src.schema.models import Base
    from src.shared.config import get_settings
    settings = get_settings()

    if not settings.has_database:
        return  # Skip if no database configured

    engine = _get_engine()
    if engine is None:
        return

    async with engine.begin() as conn:
        # Enable pgvector extension for PostgreSQL only
        if "postgresql" in settings.database_url:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)