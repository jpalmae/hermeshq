from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from conftest import DATABASE_URL, _scratch_url, requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.integration, requires_database]

BACKEND = Path(__file__).resolve().parents[1]


def _alembic(tmp_path: Path, url: str, *args: str) -> subprocess.CompletedProcess:
    ini = tmp_path / "alembic.ini"
    ini.write_text(
        f"[alembic]\nscript_location = {BACKEND / 'hermeshq' / 'alembic'}\nprepend_sys_path = {BACKEND}\n"
        "sqlalchemy.url = driver://u:p@h/d\n"
        "[loggers]\nkeys = root\n[handlers]\nkeys = console\n[formatters]\nkeys = generic\n"
        "[logger_root]\nlevel = WARN\nhandlers = console\n"
        "[handler_console]\nclass = StreamHandler\nargs = (sys.stderr,)\nlevel = NOTSET\nformatter = generic\n"
        "[formatter_generic]\nformat = %%(levelname)s %%(message)s\n"
    )
    env = {**os.environ, "DATABASE_URL": url, "JWT_SECRET": "secreto-migracion-" + "x" * 20}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ini), *args],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=300,
    )


@pytest.fixture
async def empty_db():
    admin = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    name = f"hermeshq_mig_{uuid.uuid4().hex[:10]}"
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()
    yield _scratch_url(name)
    admin = create_async_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    await admin.dispose()


NEW_TABLES = ("task_execution_attempts", "external_action_records")


async def _stamped_at_previous_head(url: str, tmp_path: Path) -> None:
    result = _alembic(tmp_path, url, "upgrade", "c9f4a7e1b320")
    assert result.returncode == 0, result.stderr[-2000:]


async def _schema(url: str) -> dict:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        cols = (
            await conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_name IN ('task_execution_attempts', 'external_action_records')"
                )
            )
        ).all()
        idx = (
            await conn.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN "
                    "('task_execution_attempts', 'external_action_records', 'tasks')"
                )
            )
        ).all()
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    await engine.dispose()
    out: dict = {"version": version, "indices": {n: d for n, d in idx}}
    for table, column in cols:
        out.setdefault(table, set()).add(column)
    return out


class TestRecoveryMigration:
    async def test_upgrade_from_production_head_creates_lease_tables_matching_models(self, empty_db, tmp_path):
        await _stamped_at_previous_head(empty_db, tmp_path)
        before = await _schema(empty_db)
        assert "task_execution_attempts" not in before
        result = _alembic(tmp_path, empty_db, "upgrade", "head")
        assert result.returncode == 0, result.stderr[-3000:]
        schema = await _schema(empty_db)

        import hermeshq.models  # noqa: F401
        from hermeshq.models.base import Base

        for table in NEW_TABLES:
            expected = {c.name for c in Base.metadata.tables[table].columns}
            assert schema[table] == expected, table
        assert schema["version"] == "e1a2b3c4d5f5"
        assert "WHERE" in schema["indices"]["uq_task_execution_attempts_live_task"]
        assert "UNIQUE" in schema["indices"]["uq_tasks_scheduled_fire_key"]

    async def test_downgrade_and_upgrade_are_reversible(self, empty_db, tmp_path):
        await _stamped_at_previous_head(empty_db, tmp_path)
        assert _alembic(tmp_path, empty_db, "upgrade", "head").returncode == 0
        down = _alembic(tmp_path, empty_db, "downgrade", "c9f4a7e1b320")
        assert down.returncode == 0, down.stderr[-3000:]
        schema = await _schema(empty_db)
        assert "task_execution_attempts" not in schema
        assert "uq_tasks_scheduled_fire_key" not in schema["indices"]
        assert _alembic(tmp_path, empty_db, "upgrade", "head").returncode == 0
        assert (await _schema(empty_db))["version"] == "e1a2b3c4d5f5"
