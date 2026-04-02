from collections.abc import AsyncGenerator

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hermeshq.config import get_settings
from hermeshq.models.base import Base

settings = get_settings()

engine = create_async_engine(settings.database_url, future=True, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def init_database() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_run_schema_updates)


def _run_schema_updates(sync_connection) -> None:
    inspector = inspect(sync_connection)
    agent_columns = {column["name"] for column in inspector.get_columns("agents")}
    if "friendly_name" not in agent_columns:
        sync_connection.execute(text("ALTER TABLE agents ADD COLUMN friendly_name VARCHAR(128)"))
        sync_connection.execute(text("UPDATE agents SET friendly_name = name WHERE friendly_name IS NULL"))
    settings_columns = {column["name"] for column in inspector.get_columns("app_settings")}
    if "app_name" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN app_name VARCHAR(128)"))
    if "app_short_name" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN app_short_name VARCHAR(48)"))
    if "logo_filename" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN logo_filename VARCHAR(255)"))
    if "favicon_filename" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN favicon_filename VARCHAR(255)"))
