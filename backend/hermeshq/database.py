import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hermeshq.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    future=True,
    echo=False,
    pool_size=30,       # Increased from 10 to handle concurrent WebSocket + API requests
    max_overflow=60,    # Increased from 20 to handle traffic spikes
    pool_timeout=30,
    pool_recycle=600,   # Reduced from 1800s to recycle stale connections faster
    pool_pre_ping=True, # Verify connections before use to avoid stale pool errors
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


async def _stamp_head() -> None:
    """Stamp the database at the latest Alembic revision without running migrations.

    Used when tables already exist (created by create_all or a prior version)
    but alembic_version is missing.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "stamp", "head"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    if result.returncode != 0:
        logger.error("Alembic stamp failed:\n%s", result.stderr)
        raise RuntimeError(f"Alembic stamp failed: {result.stderr}")
    logger.info("Alembic: stamped existing database at head")


async def _ensure_missing_objects() -> None:
    """Create any tables/columns that exist in models but not in the DB.

    Uses create_all (IF NOT EXISTS) for tables, then inspects each model
    table for missing columns and adds them with sensible defaults.
    """
    import hermeshq.models  # noqa: F401

    from hermeshq.models.base import Base

    async with engine.begin() as conn:
        def _sync_schema(sync_conn):
            Base.metadata.create_all(bind=sync_conn, checkfirst=True)

            insp = sa_inspect(sync_conn)
            for table in Base.metadata.sorted_tables:
                if not insp.has_table(table.name):
                    continue
                existing_cols = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name not in existing_cols:
                        col_type = col.type.compile(sync_conn.dialect)
                        nullable = "NULL" if col.nullable else "NOT NULL"
                        default = ""
                        if col.server_default is not None:
                            default = f" DEFAULT {col.server_default.arg}"
                        elif col.nullable:
                            default = " DEFAULT NULL"
                        stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type} {nullable}{default}'
                        sync_conn.execute(text(stmt))
                        logger.info("Added missing column %s.%s", table.name, col.name)

        await conn.run_sync(_sync_schema)


async def init_database() -> None:
    """Bring the database schema up to date, handling all starting states.

    Handles three scenarios:
    1. Fresh install (empty DB): run all Alembic migrations from scratch
    2. Existing DB with alembic_version: run pending migrations normally
    3. Existing DB without alembic_version (legacy/manual): create missing
       objects, stamp at head, then run any pending migrations
    """
    import subprocess
    import sys

    state = await _detect_db_state()
    logger.info("Database state: %s", state)

    if state == "unstamped":
        logger.warning(
            "Database has tables but no alembic_version — "
            "syncing schema and stamping at head"
        )
        await _ensure_missing_objects()
        await _stamp_head()
        return

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
