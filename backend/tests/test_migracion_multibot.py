from __future__ import annotations

import pytest
from conftest import requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_migracion_recuperacion import _alembic, empty_db  # noqa: F401

pytestmark = [pytest.mark.integration, requires_database]

TABLAS_NUEVAS = ("bot_channels", "user_channel_identities", "public_chat_delivery_prefs")
COLUMNAS_NUEVAS = {
    "public_chat_api_keys": ("bot_channel_id", "last_poll_instance"),
    "public_chat_sessions": ("user_id", "aad_object_id"),
    "scheduled_tasks": ("delivery_key_id",),
    "public_chat_notifications": ("bot_channel_id", "recipient_external_id", "recipient_aad_object_id", "origin"),
}
HEAD_ANTERIOR = "e1a2b3c4d5f2"
HEAD = "e1a2b3c4d5f8"


async def _en_head_anterior_con_datos(url, tmp_path) -> None:
    previa = _alembic(tmp_path, url, "upgrade", HEAD_ANTERIOR)
    assert previa.returncode == 0, previa.stderr[-2000:]
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, username, display_name, password_hash, auth_source, role, is_active, "
                "theme_preference, locale_preference, created_at, updated_at) VALUES "
                "('u-bridge', 'bridge-teams', 'Bridge', 'x', 'local', 'user', true, 'default', 'default', now(), now())"
            )
        )
    await engine.dispose()


async def _estado(url) -> dict:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        tablas = {
            fila[0]
            for fila in (
                await conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))
            ).all()
        }
        columnas = {
            (t, c)
            for t, c in (
                await conn.execute(
                    text("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'")
                )
            ).all()
        }
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        canales = []
        if "bot_channels" in tablas:
            canales = [
                dict(fila._mapping)
                for fila in (
                    await conn.execute(text("SELECT slug, is_default, is_active, service_user_id FROM bot_channels"))
                ).all()
            ]
        usuarios = (await conn.execute(text("SELECT count(*) FROM users"))).scalar()
    await engine.dispose()
    return {"tablas": tablas, "columnas": columnas, "version": version, "canales": canales, "usuarios": usuarios}


class TestMigracionMultibot:
    async def test_upgrade_crea_el_canal_por_defecto_y_columnas_nulas(self, empty_db, tmp_path):  # noqa: F811
        await _en_head_anterior_con_datos(empty_db, tmp_path)
        antes = await _estado(empty_db)
        assert not set(TABLAS_NUEVAS) & antes["tablas"]
        resultado = _alembic(tmp_path, empty_db, "upgrade", "head")
        assert resultado.returncode == 0, resultado.stderr[-3000:]
        despues = await _estado(empty_db)
        assert despues["version"] == HEAD
        assert set(TABLAS_NUEVAS) <= despues["tablas"]
        for tabla, columnas in COLUMNAS_NUEVAS.items():
            for columna in columnas:
                assert (tabla, columna) in despues["columnas"], (tabla, columna)
        assert despues["canales"] == [
            {"slug": "teams-principal", "is_default": True, "is_active": True, "service_user_id": "u-bridge"}
        ]

        import hermeshq.models  # noqa: F401
        from hermeshq.models.base import Base

        for tabla in TABLAS_NUEVAS:
            esperadas = {c.name for c in Base.metadata.tables[tabla].columns}
            assert {c for t, c in despues["columnas"] if t == tabla} == esperadas, tabla

    async def test_downgrade_revierte_y_upgrade_vuelve_a_aplicar(self, empty_db, tmp_path):  # noqa: F811
        await _en_head_anterior_con_datos(empty_db, tmp_path)
        assert _alembic(tmp_path, empty_db, "upgrade", "head").returncode == 0
        abajo = _alembic(tmp_path, empty_db, "downgrade", HEAD_ANTERIOR)
        assert abajo.returncode == 0, abajo.stderr[-3000:]
        estado = await _estado(empty_db)
        assert estado["version"] == HEAD_ANTERIOR and estado["usuarios"] == 1
        assert not set(TABLAS_NUEVAS) & estado["tablas"]
        for tabla, columnas in COLUMNAS_NUEVAS.items():
            for columna in columnas:
                assert (tabla, columna) not in estado["columnas"], (tabla, columna)
        assert _alembic(tmp_path, empty_db, "upgrade", "head").returncode == 0
        otra_vez = await _estado(empty_db)
        assert otra_vez["version"] == HEAD and len(otra_vez["canales"]) == 1
