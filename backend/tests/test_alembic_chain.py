from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from conftest import DATABASE_URL, requires_database
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
INITIAL_MIGRATION = BACKEND_DIR / "hermeshq/alembic/versions/d39fa7cf25af_initial_schema_from_models.py"
HEAD_REVISION = "r2s3t4u5v6w7"
LEGACY_HEAD_REVISION = "q1r2s3t4u5v6"


def _run(coroutine):
    return asyncio.run(coroutine)


def _database_url(database_name: str) -> str:
    return make_url(DATABASE_URL).set(database=database_name).render_as_string(hide_password=False)


async def _create_database(database_name: str) -> None:
    engine = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        await engine.dispose()


async def _drop_database(database_name: str) -> None:
    engine = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :database_name"),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        await engine.dispose()


async def _schema_state(database_url: str) -> tuple[str | None, set[str]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:

            def inspect_schema(sync_connection) -> tuple[str | None, set[str]]:
                revision = MigrationContext.configure(sync_connection).get_current_revision()
                tables = set(inspect(sync_connection).get_table_names())
                return revision, tables

            return await connection.run_sync(inspect_schema)
    finally:
        await engine.dispose()


async def _prepare_legacy_schema(database_url: str) -> None:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            for table_name, column_names in (
                ("agents", ("runtime_profile", "integration_configs", "is_system_agent", "is_archived")),
                ("app_settings", ("enabled_integration_packages",)),
                ("tasks", ("board_column", "board_order", "board_manual")),
                ("users", ("auth_source", "role", "is_active", "theme_preference", "locale_preference")),
            ):
                for column_name in column_names:
                    await connection.execute(
                        text(f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" DROP NOT NULL')
                    )
            for index_name in (
                "ix_agents_friendly_name",
                "ix_agents_hermes_version",
                "ix_agents_is_archived",
                "ix_agents_is_system_agent",
                "ix_agents_runtime_profile",
                "ix_tasks_board_column",
                "ix_tasks_board_order",
                "ix_users_role",
            ):
                await connection.execute(text(f'DROP INDEX IF EXISTS "{index_name}"'))
            await connection.execute(
                text(
                    """
                    INSERT INTO users (
                        id, username, display_name, password_hash, auth_source, role,
                        is_active, theme_preference, locale_preference, created_at, updated_at
                    ) VALUES (
                        'legacy-user', 'legacy-user', 'Legacy User', 'unused', NULL, NULL,
                        NULL, NULL, NULL, now(), now()
                    )
                    """
                )
            )
    finally:
        await engine.dispose()


async def _legacy_user_defaults(database_url: str) -> tuple[str, str, bool, str, str]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT auth_source, role, is_active, theme_preference, locale_preference
                        FROM users WHERE id = 'legacy-user'
                        """
                    )
                )
            ).one()
            return row._tuple()
    finally:
        await engine.dispose()


def _alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "hermeshq/alembic"))
    return config


def test_initial_migration_is_frozen() -> None:
    source = INITIAL_MIGRATION.read_text()

    assert "hermeshq.models" not in source
    assert "Base.metadata" not in source
    assert source.count("op.create_table(") == 19


@pytest.mark.integration
@requires_database
def test_full_chain_upgrades_downgrades_and_matches_models() -> None:
    database_name = f"hermeshq_alembic_{uuid.uuid4().hex[:10]}"
    database_url = _database_url(database_name)
    config = _alembic_config()

    _run(_create_database(database_name))
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DATABASE_URL", database_url)

            command.upgrade(config, "head")
            revision, tables = _run(_schema_state(database_url))
            assert revision == HEAD_REVISION
            assert {"users", "agents", "tasks", "permission_policies"} <= tables
            command.check(config)

            command.downgrade(config, "base")
            revision, tables = _run(_schema_state(database_url))
            assert revision is None
            assert tables == {"alembic_version"}

            command.upgrade(config, "head")
            revision, _ = _run(_schema_state(database_url))
            assert revision == HEAD_REVISION
            command.check(config)
    finally:
        _run(_drop_database(database_name))


@pytest.mark.integration
@requires_database
def test_legacy_head_is_reconciled_with_models() -> None:
    database_name = f"hermeshq_legacy_{uuid.uuid4().hex[:10]}"
    database_url = _database_url(database_name)
    config = _alembic_config()

    _run(_create_database(database_name))
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("DATABASE_URL", database_url)

            command.upgrade(config, LEGACY_HEAD_REVISION)
            _run(_prepare_legacy_schema(database_url))
            command.upgrade(config, "head")

            revision, _ = _run(_schema_state(database_url))
            assert revision == HEAD_REVISION
            assert _run(_legacy_user_defaults(database_url)) == ("local", "user", True, "default", "default")
            command.check(config)
    finally:
        _run(_drop_database(database_name))
