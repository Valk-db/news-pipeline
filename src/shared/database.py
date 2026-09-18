from contextlib import asynccontextmanager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from src.shared.config import get_settings


_engine = None
_async_session_maker = None


def _get_engine():
    """Lazily create engine for serverless environments."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            poolclass=NullPool,  # Supabase/Neon work better without pooling
            echo=False,
        )
    return _engine


def _get_session_maker():
    """Lazily create session maker for serverless environments."""
    global _async_session_maker
    if _async_session_maker is None:
        _async_session_maker = async_sessionmaker(
            _get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _async_session_maker


@asynccontextmanager
async def get_session() -> AsyncSession:
    async with _get_session_maker()() as session:
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

    async with _get_engine().begin() as conn:
        # Enable pgvector extension for PostgreSQL only
        if "postgresql" in settings.database_url:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)