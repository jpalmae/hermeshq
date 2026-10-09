"""Shared pytest fixtures.

Integration fixtures (real Postgres) are used by tests marked with
``pytest.mark.integration``; they skip automatically when no database is
reachable, so the unit suite keeps working without Postgres.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough")
os.environ.setdefault("FERNET_KEY", "test-fernet-key")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("DEBUG", "true")

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://hermeshq:hermeshq@localhost:5432/hermeshq",
)


def _database_reachable() -> bool:
    async def _check() -> bool:
        try:
            engine = create_async_engine(DATABASE_URL)
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await engine.dispose()
            return True
        except Exception:  # noqa: BLE001  # any failure means "no DB available"
            return False

    try:
        return asyncio.run(_check())
    except RuntimeError:
        return False


_DB_AVAILABLE: bool | None = None


def database_available() -> bool:
    global _DB_AVAILABLE
    if _DB_AVAILABLE is None:
        _DB_AVAILABLE = _database_reachable()
    return _DB_AVAILABLE


requires_database = pytest.mark.skipif(not database_available(), reason="PostgreSQL not reachable")


def _scratch_url(db_name: str) -> str:
    return DATABASE_URL.rsplit("/", 1)[0] + f"/{db_name}"


@pytest_asyncio.fixture
async def db_engine():
    """Create a scratch database with the full schema and yield (engine, name)."""
    admin_engine = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    db_name = f"hermeshq_test_{uuid.uuid4().hex[:10]}"
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await admin_engine.dispose()

    engine = create_async_engine(_scratch_url(db_name))
    import hermeshq.models  # noqa: F401  (register all tables)
    from hermeshq.models.base import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine, db_name
    await engine.dispose()

    admin_engine = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await admin_engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """Yield an AsyncSession bound to the scratch database."""
    engine, _ = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
