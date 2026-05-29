import logging
import subprocess
import sys
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hermeshq.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    future=True,
    echo=False,  # Set to True for debugging connection issues
    pool_size=30,
    max_overflow=60,
    pool_timeout=30,
    pool_recycle=600,  # Recycle connections after 10 minutes
    pool_pre_ping=True,
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def init_database() -> None:
    """Run Alembic migrations to bring the database schema up to date.

    Legacy inline schema updates (_run_schema_updates) were removed in
    v2026.5.22.1.  All schema changes are now managed exclusively through
    Alembic migrations in ``hermeshq/alembic/versions/``.
    """
    import asyncio

    loop = asyncio.get_running_loop()

    def _run_alembic_upgrade() -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
            capture_output=True,
            text=True,
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                logger.info("[alembic] %s", line)
        if result.returncode != 0:
            logger.error("[alembic] %s", result.stderr)
            raise RuntimeError(f"Alembic upgrade failed: {result.stderr}")

    await loop.run_in_executor(None, _run_alembic_upgrade)
    logger.info("Alembic migrations applied successfully")
