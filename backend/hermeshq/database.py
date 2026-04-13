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
    if "theme_preference" not in user_columns:
        sync_connection.execute(text("ALTER TABLE users ADD COLUMN theme_preference VARCHAR(16)"))
        sync_connection.execute(text("UPDATE users SET theme_preference = 'default' WHERE theme_preference IS NULL"))
    if "locale_preference" not in user_columns:
        sync_connection.execute(text("ALTER TABLE users ADD COLUMN locale_preference VARCHAR(16)"))
        sync_connection.execute(text("UPDATE users SET locale_preference = 'default' WHERE locale_preference IS NULL"))
    if "avatar_filename" not in user_columns:
        sync_connection.execute(text("ALTER TABLE users ADD COLUMN avatar_filename VARCHAR(255)"))
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
    if "avatar_filename" not in agent_columns:
        sync_connection.execute(text("ALTER TABLE agents ADD COLUMN avatar_filename VARCHAR(255)"))
    if "runtime_profile" not in agent_columns:
        sync_connection.execute(text("ALTER TABLE agents ADD COLUMN runtime_profile VARCHAR(32)"))
        sync_connection.execute(text("UPDATE agents SET runtime_profile = 'standard' WHERE runtime_profile IS NULL"))
    if "integration_configs" not in agent_columns:
        sync_connection.execute(text("ALTER TABLE agents ADD COLUMN integration_configs JSON"))
        sync_connection.execute(text("UPDATE agents SET integration_configs = '{}' WHERE integration_configs IS NULL"))
    settings_columns = {column["name"] for column in inspector.get_columns("app_settings")}
    if "app_name" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN app_name VARCHAR(128)"))
    if "app_short_name" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN app_short_name VARCHAR(48)"))
    if "theme_mode" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN theme_mode VARCHAR(16)"))
    if "default_locale" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN default_locale VARCHAR(8)"))
    if "default_tui_skin" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN default_tui_skin VARCHAR(128)"))
    if "enabled_integration_packages" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN enabled_integration_packages JSON"))
        sync_connection.execute(
            text(
                """
                UPDATE app_settings
                SET enabled_integration_packages = '[]'
                WHERE enabled_integration_packages IS NULL
                """
            )
        )
    if "tui_skin_filename" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN tui_skin_filename VARCHAR(255)"))
    if "logo_filename" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN logo_filename VARCHAR(255)"))
    if "favicon_filename" not in settings_columns:
        sync_connection.execute(text("ALTER TABLE app_settings ADD COLUMN favicon_filename VARCHAR(255)"))
    if not inspector.has_table("providers"):
        sync_connection.execute(
            text(
                """
                CREATE TABLE providers (
                    slug VARCHAR(64) PRIMARY KEY,
                    name VARCHAR(128) NOT NULL,
                    runtime_provider VARCHAR(64) NOT NULL,
                    auth_type VARCHAR(32) NOT NULL DEFAULT 'api_key',
                    base_url VARCHAR(512) NULL,
                    default_model VARCHAR(255) NULL,
                    description TEXT NULL,
                    docs_url VARCHAR(512) NULL,
                    secret_placeholder VARCHAR(128) NULL,
                    supports_secret_ref BOOLEAN NOT NULL DEFAULT TRUE,
                    supports_custom_base_url BOOLEAN NOT NULL DEFAULT TRUE,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    sort_order INTEGER NOT NULL DEFAULT 100,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        sync_connection.execute(text("CREATE INDEX ix_providers_runtime_provider ON providers(runtime_provider)"))
    if not inspector.has_table("messaging_channels"):
        sync_connection.execute(
            text(
                """
                CREATE TABLE messaging_channels (
                    id VARCHAR(36) PRIMARY KEY,
                    agent_id VARCHAR(36) NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    platform VARCHAR(32) NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    mode VARCHAR(32) NOT NULL DEFAULT 'bidirectional',
                    secret_ref VARCHAR(128) NULL,
                    allowed_user_ids JSON NOT NULL DEFAULT '[]'::json,
                    home_chat_id VARCHAR(128) NULL,
                    home_chat_name VARCHAR(128) NULL,
                    require_mention BOOLEAN NOT NULL DEFAULT FALSE,
                    free_response_chat_ids JSON NOT NULL DEFAULT '[]'::json,
                    unauthorized_dm_behavior VARCHAR(32) NOT NULL DEFAULT 'pair',
                    status VARCHAR(20) NOT NULL DEFAULT 'stopped',
                    last_error TEXT NULL,
                    metadata_json JSON NOT NULL DEFAULT '{}'::json,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_messaging_channels_agent_platform UNIQUE (agent_id, platform)
                )
                """
            )
        )
        sync_connection.execute(text("CREATE INDEX ix_messaging_channels_agent_id ON messaging_channels(agent_id)"))
        sync_connection.execute(text("CREATE INDEX ix_messaging_channels_platform ON messaging_channels(platform)"))
        sync_connection.execute(text("CREATE INDEX ix_messaging_channels_status ON messaging_channels(status)"))
    if not inspector.has_table("conversation_threads"):
        sync_connection.execute(
            text(
                """
                CREATE TABLE conversation_threads (
                    id VARCHAR(36) PRIMARY KEY,
                    agent_id VARCHAR(36) NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    title VARCHAR(255) NULL,
                    last_task_id VARCHAR(36) NULL REFERENCES tasks(id) ON DELETE SET NULL,
                    notes TEXT NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_conversation_threads_agent_user UNIQUE (agent_id, user_id)
                )
                """
            )
        )
        sync_connection.execute(text("CREATE INDEX ix_conversation_threads_agent_id ON conversation_threads(agent_id)"))
        sync_connection.execute(text("CREATE INDEX ix_conversation_threads_user_id ON conversation_threads(user_id)"))
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "board_column" not in task_columns:
        sync_connection.execute(text("ALTER TABLE tasks ADD COLUMN board_column VARCHAR(32)"))
        sync_connection.execute(
            text(
                """
                UPDATE tasks
                SET board_column = CASE
                    WHEN status = 'running' THEN 'running'
                    WHEN status = 'completed' THEN 'done'
                    WHEN status IN ('failed', 'cancelled') THEN 'failed'
                    ELSE 'inbox'
                END
                WHERE board_column IS NULL
                """
            )
        )
    if "board_order" not in task_columns:
        sync_connection.execute(text("ALTER TABLE tasks ADD COLUMN board_order BIGINT"))
        sync_connection.execute(
            text(
                """
                UPDATE tasks
                SET board_order = FLOOR(
                    EXTRACT(EPOCH FROM COALESCE(completed_at, started_at, queued_at, CURRENT_TIMESTAMP)) * 1000
                )::BIGINT
                WHERE board_order IS NULL
                """
            )
        )
    if "board_manual" not in task_columns:
        sync_connection.execute(text("ALTER TABLE tasks ADD COLUMN board_manual BOOLEAN DEFAULT FALSE"))
        sync_connection.execute(text("UPDATE tasks SET board_manual = FALSE WHERE board_manual IS NULL"))
