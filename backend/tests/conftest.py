"""Shared pytest fixtures.

Integration fixtures (real Postgres) are used by tests marked with
``pytest.mark.integration``; they skip automatically when no database is
reachable, so the unit suite keeps working without Postgres.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (_BACKEND_ROOT, os.environ.get("PYTHONPATH")) if p)

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


@pytest.fixture(autouse=True)
def _entorno_y_carpeta_restaurados():
    entorno = dict(os.environ)
    try:
        carpeta = os.getcwd()
    except OSError:
        carpeta = None
    yield
    if dict(os.environ) != entorno:
        os.environ.clear()
        os.environ.update(entorno)
    if carpeta:
        try:
            if os.getcwd() != carpeta:
                os.chdir(carpeta)
        except OSError:
            os.chdir(carpeta)


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


def _is_live_e2e(name: str) -> bool:
    return name.startswith("test_e2e_") or name.endswith("_e2e.py")


def _uses_live_fixture(item) -> bool:
    info = getattr(item, "_fixtureinfo", None)
    for name in getattr(item, "fixturenames", ()):
        for definition in info.name2fixturedefs.get(name, ()) if info else ():
            module = getattr(getattr(definition, "func", None), "__module__", "") or ""
            if _is_live_e2e(module.rsplit(".", 1)[-1] + ".py") or name.startswith("stack"):
                return True
    return False


def pytest_collection_modifyitems(config, items):
    if os.environ.get("HQ_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="live multi-process E2E; set HQ_E2E=1 to run")
    for item in items:
        if _is_live_e2e(item.path.name) or _uses_live_fixture(item):
            item.add_marker(pytest.mark.e2e)
            item.add_marker(skip)


async def run_queued(supervisor, task_id: str) -> None:
    from datetime import timedelta
    from uuid import uuid4

    from sqlalchemy import update

    from hermeshq.models.base import utcnow
    from hermeshq.models.task import Task

    token = str(uuid4())
    now = utcnow()
    async with supervisor.session_factory() as session:
        await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status.in_(("queued", "running")))
            .values(
                status="running",
                claim_token=token,
                claimed_by=getattr(supervisor, "_worker_id", "test-worker"),
                claimed_at=now,
                lease_expires_at=now + timedelta(minutes=5),
                attempt_count=Task.attempt_count + 1,
                started_at=now,
                execution_lane=token[:16],
            )
        )
        await session.commit()
    await supervisor._run_task(task_id, token)
