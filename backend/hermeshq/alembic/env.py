"""
Alembic env.py for async SQLAlchemy migrations.

This module configures Alembic to work with the project's async SQLAlchemy
engine and declarative models.  It imports ALL model modules so that
`alembic revision --autogenerate` can detect every table and column.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# --- Project imports --------------------------------------------------------
from hermeshq.config import get_settings  # noqa: E402
from hermeshq.models.base import Base  # noqa: E402  — declarative base

# Import every model module so their tables are registered on Base.metadata.
# This is required for autogenerate to "see" them.
import hermeshq.models.activity  # noqa: F401, E402
import hermeshq.models.agent  # noqa: F401, E402
import hermeshq.models.agent_assignment  # noqa: F401, E402
import hermeshq.models.app_settings  # noqa: F401, E402
import hermeshq.models.conversation_thread  # noqa: F401, E402
import hermeshq.models.hermes_version  # noqa: F401, E402
import hermeshq.models.integration_draft  # noqa: F401, E402
import hermeshq.models.mcp_access  # noqa: F401, E402
import hermeshq.models.message  # noqa: F401, E402
import hermeshq.models.messaging_channel  # noqa: F401, E402
import hermeshq.models.node  # noqa: F401, E402
import hermeshq.models.provider  # noqa: F401, E402
import hermeshq.models.scheduled_task  # noqa: F401, E402
import hermeshq.models.secret  # noqa: F401, E402
import hermeshq.models.task  # noqa: F401, E402
import hermeshq.models.template  # noqa: F401, E402
import hermeshq.models.terminal_session  # noqa: F401, E402
import hermeshq.models.user  # noqa: F401, E402

# --- Alembic config ---------------------------------------------------------

config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The MetaData object for autogenerate support.
target_metadata = Base.metadata


def _get_url() -> str:
    """Resolve the database URL from project settings (honours .env)."""
    settings = get_settings()
    return settings.database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine.  Calls to
    ``context.execute()`` emit the given string to the script output.
    """
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Run migrations using a connection already obtained from the async engine."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations inside a connection."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        url=_get_url(),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (async)."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
