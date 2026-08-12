import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hermeshq.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    future=True,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=600,  # Reduced from 1800s to recycle stale connections faster
    pool_pre_ping=True,  # Verify connections before use to avoid stale pool errors
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def _detect_db_state() -> str:
    """Detect the current database state relative to Alembic.

    Returns one of:
      - "empty"       — no application tables exist (fresh install)
      - "stamped"     — alembic_version table exists with a revision
      - "unstamped"   — application tables exist but no alembic_version
    """
    async with engine.connect() as conn:

        def _inspect(sync_conn):
            insp = sa_inspect(sync_conn)
            tables = set(insp.get_table_names())
            has_alembic = "alembic_version" in tables
            has_app_tables = bool(tables & {"users", "agents", "tasks"})
            if has_alembic:
                return "stamped"
            if has_app_tables:
                return "unstamped"
            return "empty"

        return await conn.run_sync(_inspect)


async def init_database() -> None:
    """Bring a fresh or Alembic-managed database schema up to date."""
    import subprocess
    import sys

    state = await _detect_db_state()
    logger.info("Database state: %s", state)

    if state == "unstamped":
        raise RuntimeError(
            "Database contains HermesHQ tables but has no Alembic revision. "
            "Refusing to stamp it at head because that can skip data migrations and constraints. "
            "Back up the database and run the explicit legacy adoption procedure."
        )

    # For "empty" and "stamped" states, run alembic upgrade head normally.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    if result.returncode != 0:
        logger.error("Alembic migration failed:\n%s", result.stderr)
        raise RuntimeError(f"Alembic migration failed: {result.stderr}")
    logger.info("Alembic migrations applied successfully")
