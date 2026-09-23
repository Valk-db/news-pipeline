from contextlib import asynccontextmanager
from typing import AsyncGenerator
from uuid import uuid4
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from src.shared.config import get_settings


_engine = None
_async_session_maker = None

# libpq-style URL parameters that asyncpg rejects (it would raise TypeError on connect).
_LIBPQ_ONLY_PARAMS = {"pgbouncer", "channel_binding", "connect_timeout", "options", "supa"}
_VALID_SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


def prepare_database_url(raw_url: str):
    """Turn whatever was pasted into DATABASE_URL into (url, connect_args) that asyncpg accepts.

    Supabase/Neon dashboards hand out plain ``postgresql://`` URLs (sometimes with
    ``?sslmode=require``). SQLAlchemy's async engine needs ``postgresql+asyncpg://`` and asyncpg
    does not understand ``sslmode``, so both are translated here instead of failing at request time.
    """
    url = make_url(raw_url.strip())
    is_sqlite = url.drivername.startswith("sqlite")
    if url.drivername in ("postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"):
        url = url.set(drivername="postgresql+asyncpg")

    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    for param in _LIBPQ_ONLY_PARAMS:
        query.pop(param, None)
    url = url.set(query=query)

    connect_args = {}
    if not is_sqlite:
        # Safe behind Supabase's transaction pooler (port 6543) / PgBouncer, which cannot keep
        # named prepared statements between transactions. Only applies to asyncpg (PostgreSQL).
        connect_args = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
        }
    if isinstance(sslmode, str) and sslmode in _VALID_SSL_MODES:
        connect_args["ssl"] = sslmode
    return url, connect_args


def describe_database_url(raw_url: str) -> dict:
    """Non-secret summary of DATABASE_URL for diagnostics (never includes user, password or host)."""
    try:
        parsed = make_url(raw_url.strip())
    except Exception as exc:  # e.g. unescaped '@' or '#' in the password
        return {"parse_error": f"{type(exc).__name__}: could not parse DATABASE_URL"}
    host = parsed.host or ""
    return {
        "driver_as_pasted": parsed.drivername,
        "port": parsed.port,
        "host_kind": (
            "supabase-pooler" if "pooler.supabase.com" in host
            else "supabase-direct (IPv6 only, fails on Vercel)" if host.startswith("db.") and host.endswith(".supabase.co")
            else "other"
        ),
        "has_query_params": sorted(parsed.query.keys()),
    }


def _get_engine():
    """Lazily create engine for serverless environments. Returns None if DB not configured."""
    global _engine
    if _engine is None:
        settings = get_settings()
        if not settings.has_database:
            return None
        url, connect_args = prepare_database_url(settings.database_url)
        _engine = create_async_engine(
            url,
            poolclass=NullPool,  # Supabase/Neon work better without pooling
            connect_args=connect_args,
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
    """Create tables."""
    from src.schema.models import Base
    from src.shared.config import get_settings
    settings = get_settings()

    if not settings.has_database:
        return  # Skip if no database configured

    engine = _get_engine()
    if engine is None:
        return

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)