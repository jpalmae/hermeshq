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
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "role" not in user_columns:
        sync_connection.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(16)"))
        sync_connection.execute(text("UPDATE users SET role = 'admin' WHERE username = 'admin'"))
        sync_connection.execute(text("UPDATE users SET role = 'user' WHERE role IS NULL"))
    if "is_active" not in user_columns:
        sync_connection.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT TRUE"))
        sync_connection.execute(text("UPDATE users SET is_active = TRUE WHERE is_active IS NULL"))
    if not inspector.has_table("agent_assignments"):
        sync_connection.execute(
            text(
                """
                CREATE TABLE agent_assignments (
                    id VARCHAR(36) PRIMARY KEY,
                    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    agent_id VARCHAR(36) NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    assigned_by VARCHAR(36) NULL REFERENCES users(id) ON DELETE SET NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_agent_assignments_user_agent UNIQUE (user_id, agent_id)
                )
                """
            )
        )
        sync_connection.execute(text("CREATE INDEX ix_agent_assignments_user_id ON agent_assignments(user_id)"))
        sync_connection.execute(text("CREATE INDEX ix_agent_assignments_agent_id ON agent_assignments(agent_id)"))
    agent_columns = {column["name"] for column in inspector.get_columns("agents")}
    if "friendly_name" not in agent_columns:
        sync_connection.execute(text("ALTER TABLE agents ADD COLUMN friendly_name VARCHAR(128)"))
        sync_connection.execute(text("UPDATE agents SET friendly_name = name WHERE friendly_name IS NULL"))
    settings_columns = {column["name"] for column in inspector.get_columns("app_settings")}
    if "app_name" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN app_name VARCHAR(128)"))
    if "app_short_name" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN app_short_name VARCHAR(48)"))
    if "theme_mode" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN theme_mode VARCHAR(16)"))
    if "logo_filename" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN logo_filename VARCHAR(255)"))
    if "favicon_filename" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN favicon_filename VARCHAR(255)"))
