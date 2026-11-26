from __future__ import annotations

import asyncio

import pytest
from conftest import requires_database
from test_e2e_calendario_teams import _aprobar, _escrituras, _llamar, _plugins
from test_e2e_calendario_teams import mundo as mundo_ct  # noqa: F401
from test_e2e_correo_central import _pedido_y_aprobado, mundo  # noqa: F401

pytestmark = [pytest.mark.integration, requires_database]


class TestCorreo:
    async def test_misma_llamada_sin_id_tras_aprobar_envia_una_vez(self, mundo):  # noqa: F811
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        r = await asyncio.to_thread(mundo.enviar, plugin, aprobado_por_el_usuario=True)
        assert r["success"] is True, r
        assert mundo.envios_graph() == 1
        fila = await mundo.aprobacion(pedido["aprobacion_id"])
        assert fila["estado"] == "ejecutada"
        otra = await asyncio.to_thread(mundo.enviar, plugin, aprobado_por_el_usuario=True)
        assert otra.get("ya_enviado") is True and otra["aprobacion_id"] == pedido["aprobacion_id"], otra
        assert mundo.envios_graph() == 1

    async def test_contenido_distinto_sin_id_pide_aprobacion_nueva(self, mundo):  # noqa: F811
        task_id, lease, plugin, pedido = await _pedido_y_aprobado(mundo)
        r = await asyncio.to_thread(mundo.enviar, plugin, subject="Otro asunto", aprobado_por_el_usuario=True)
        assert r.get("requiere_aprobacion") is True and r["aprobacion_id"] != pedido["aprobacion_id"]
        assert mundo.envios_graph() == 0

    async def test_pendiente_sin_aprobar_no_se_ejecuta(self, mundo):  # noqa: F811
        task_id, lease = await mundo.tarea()
        plugin = mundo.plugin_para(task_id, lease)
        p1 = await asyncio.to_thread(mundo.enviar, plugin)
        p2 = await asyncio.to_thread(mundo.enviar, plugin, aprobado_por_el_usuario=True)
        assert p2.get("requiere_aprobacion") is True and p2["aprobacion_id"] == p1["aprobacion_id"]
        assert mundo.envios_graph() == 0


class TestCalendario:
    async def test_misma_reunion_sin_id_tras_aprobar_se_crea_una_vez(self, mundo_ct):  # noqa: F811
        m = mundo_ct
        _, _, cal, _ = await _plugins(m)
        args = {"subject": "Revisión", "start": "2026-11-25T15:00:00", "end": "2026-11-25T16:00:00", "timezone": "UTC"}
        pedido = await _llamar(cal, "_create_event_tool", **args)
        await _aprobar(m, pedido)
        r = await _llamar(cal, "_create_event_tool", **args)
        assert r["success"] is True, r
        assert len(_escrituras(m, "POST")) == 1
        assert (await m.aprobacion(pedido["aprobacion_id"]))["estado"] == "ejecutada"
