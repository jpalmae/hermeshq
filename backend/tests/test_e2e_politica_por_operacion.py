from __future__ import annotations

import json

import httpx
import pytest
from conftest import requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_e2e_calendario_teams import POLITICA, _aprobar, _GraphCalTeams, _llamar, _plugins
from test_e2e_correo_central import _login, _login_como, _Mundo

from hermeshq.core.security import create_agent_service_token
from hermeshq.models.agent import Agent

pytestmark = [pytest.mark.integration, requires_database]

CHAT = "19:chat-directo@thread.v2"
EVENTO = {"subject": "Revisión", "start": "2026-11-25T15:00:00", "end": "2026-11-25T16:00:00", "timezone": "UTC"}


async def _mundo(db_engine, tmp_path, monkeypatch, politica_tenant: dict | None = None):
    engine, db_name = db_engine
    graph = type(
        "GraphCT", (_GraphCalTeams,), {"escrituras": [], "miembros": ["admin@hq.example", "colega@hq.example"]}
    )
    m = _Mundo(
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
        db_name,
        tmp_path,
        monkeypatch,
        graph_cls=graph,
        politica=POLITICA,
    )
    if politica_tenant is not None:
        base = m.env_backend
        m.env_backend = lambda: {**base(), "APPROVAL_POLICY_DEFAULT": json.dumps(politica_tenant)}
    await m.arrancar_backend()
    await m.sembrar()
    return m


@pytest.fixture
async def mundo(db_engine, tmp_path, monkeypatch):
    m = await _mundo(db_engine, tmp_path, monkeypatch)
    try:
        yield m
    finally:
        m.cerrar()


@pytest.fixture
async def mundo_tenant(db_engine, tmp_path, monkeypatch):
    m = await _mundo(
        db_engine, tmp_path, monkeypatch, {"calendario.eliminar": "bloqueada", "teams.mensaje": "bloqueada"}
    )
    try:
        yield m
    finally:
        m.cerrar()


async def _politica(m: _Mundo, cambios: dict, usuario: str = "admin") -> httpx.Response:
    headers = await _login_como(m.backend.base, usuario)
    async with httpx.AsyncClient() as c:
        return await c.put(
            f"{m.backend.base}/api/agents/{m.agent_id}/aprobacion-central/politica",
            headers=headers,
            json={"politica": cambios},
        )


async def _ver(m: _Mundo) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{m.backend.base}/api/agents/{m.agent_id}/aprobacion-central", headers=await _login(m.backend.base)
        )
    assert r.status_code == 200, r.text
    return r.json()


async def _aprobaciones(m: _Mundo) -> int:
    async with m.maker() as s:
        return (await s.execute(text("SELECT count(*) FROM approvals"))).scalar_one()


class TestPoliticaPorOperacion:
    async def test_por_defecto_lecturas_sin_aprobacion_y_escrituras_con_aprobacion(self, mundo):
        _, _, cal, teams = await _plugins(mundo)
        eventos = await _llamar(cal, "_list_events_tool", start="2026-11-01T00:00:00Z", end="2026-11-30T00:00:00Z")
        chats = await _llamar(teams, "_list_chats_tool")
        assert eventos["success"] is True and chats.get("success", True) is not False, (eventos, chats)
        assert await _aprobaciones(mundo) == 0
        pedido = await _llamar(cal, "_create_event_tool", **EVENTO)
        assert pedido["requiere_aprobacion"] is True
        politica = (await _ver(mundo))["politica_efectiva"]
        assert set(politica.values()) == {"aprobacion"} and len(politica) == 9 and "onedrive.compartir" in politica

    async def test_bloquear_un_tipo_no_afecta_a_los_demas_ni_a_las_lecturas(self, mundo):
        r = await _politica(mundo, {"teams.mensaje": "bloqueada"})
        assert r.status_code == 200, r.text
        _, _, cal, teams = await _plugins(mundo)
        bloqueado = await _llamar(teams, "_send_chat_message_tool", chat_id=CHAT, message="hola")
        assert bloqueado["success"] is False and bloqueado["codigo"] == "operacion_bloqueada", bloqueado
        assert await _aprobaciones(mundo) == 0
        assert not mundo.graph.escrituras
        lectura = await _llamar(teams, "_list_chats_tool")
        assert lectura.get("success", True) is not False
        pedido = await _llamar(cal, "_create_event_tool", **EVENTO)
        assert pedido["requiere_aprobacion"] is True

    async def test_bloqueo_despues_de_pedir_expira_la_pendiente_y_no_escribe(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        pedido = await _llamar(cal, "_create_event_tool", **EVENTO)
        aprob_id = pedido["aprobacion_id"]
        r = await _politica(mundo, {"calendario.crear": "bloqueada"})
        assert r.json()["expiradas"] == [aprob_id]
        assert (await mundo.decidir(aprob_id)).status_code != 200
        intento = await _llamar(cal, "_create_event_tool", **EVENTO, aprobacion_id=aprob_id)
        assert intento["success"] is False and intento["codigo"] == "operacion_bloqueada", intento
        assert not mundo.graph.escrituras
        assert (await mundo.aprobacion(aprob_id))["estado"] == "expirada"

    async def test_bloqueo_por_politica_al_ejecutar_aunque_no_se_expire_antes(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        pedido = await _llamar(cal, "_create_event_tool", **EVENTO)
        async with mundo.maker() as s:
            await s.execute(
                text("UPDATE agents SET approval_policy = :p WHERE id = :i"),
                {"p": json.dumps({"calendario.crear": "bloqueada"}), "i": mundo.agent_id},
            )
            await s.commit()
        aprob_id = await _aprobar(mundo, pedido)
        intento = await _llamar(cal, "_create_event_tool", **EVENTO, aprobacion_id=aprob_id)
        assert intento["codigo"] == "operacion_bloqueada"
        assert not mundo.graph.escrituras
        assert (await mundo.aprobacion(aprob_id))["estado"] == "expirada"

    async def test_solo_expira_las_aprobaciones_del_tipo_bloqueado(self, mundo):
        _, _, cal, teams = await _plugins(mundo)
        cal_pedido = await _llamar(cal, "_create_event_tool", **EVENTO)
        teams_pedido = await _llamar(teams, "_send_chat_message_tool", chat_id=CHAT, message="hola")
        r = await _politica(mundo, {"teams.mensaje": "bloqueada"})
        assert r.json()["expiradas"] == [teams_pedido["aprobacion_id"]]
        assert (await mundo.aprobacion(cal_pedido["aprobacion_id"]))["estado"] == "pendiente"

    async def test_no_se_bloquea_con_una_ejecucion_en_curso(self, mundo):
        _, _, cal, _ = await _plugins(mundo)
        pedido = await _llamar(cal, "_create_event_tool", **EVENTO)
        mundo.graph.modo = "timeout_tras_aceptar"
        await _aprobar(mundo, pedido)
        r = await _politica(mundo, {"calendario.crear": "bloqueada"})
        assert r.status_code == 409 and r.json()["detail"]["codigo"] == "ejecucion_en_curso"
        assert (await _ver(mundo))["politica_agente"] == {}

    async def test_desbloquear_con_null_vuelve_a_heredar(self, mundo):
        await _politica(mundo, {"teams.mensaje": "bloqueada"})
        r = await _politica(mundo, {"teams.mensaje": None})
        assert r.json()["politica_agente"] == {} and r.json()["politica_efectiva"]["teams.mensaje"] == "aprobacion"


class TestPrecedenciaTenant:
    async def test_tenant_bloquea_y_el_agente_puede_volver_a_exigir_aprobacion(self, mundo_tenant):
        ver = await _ver(mundo_tenant)
        assert ver["politica_tenant"] == {"calendario.eliminar": "bloqueada", "teams.mensaje": "bloqueada"}
        _, _, cal, teams = await _plugins(mundo_tenant)
        assert (await _llamar(cal, "_delete_event_tool", event_id="evt-00001"))["codigo"] == "operacion_bloqueada"
        assert (await _llamar(teams, "_send_chat_message_tool", chat_id=CHAT, message="x"))["codigo"] == (
            "operacion_bloqueada"
        )
        r = await _politica(mundo_tenant, {"calendario.eliminar": "aprobacion"})
        assert r.json()["politica_efectiva"]["calendario.eliminar"] == "aprobacion"
        assert r.json()["politica_efectiva"]["teams.mensaje"] == "bloqueada"
        pedido = await _llamar(cal, "_delete_event_tool", event_id="evt-00001")
        assert pedido["requiere_aprobacion"] is True


class TestSinBypassNiAutoconcesion:
    async def test_politica_invalida_se_rechaza(self, mundo):
        r = await _politica(mundo, {"correo.enviar": "sin_aprobacion"})
        assert r.status_code == 422 and r.json()["detail"]["modos_invalidos"] == ["correo.enviar"]
        r = await _politica(mundo, {"correo.borrar_todo": "aprobacion"})
        assert r.status_code == 422 and r.json()["detail"]["operaciones_desconocidas"] == ["correo.borrar_todo"]

    async def test_usuario_no_admin_ni_agente_cambian_la_politica(self, mundo):
        async with mundo.maker() as s:
            await s.execute(text("UPDATE users SET role = 'user' WHERE username = 'otra-usuaria'"))
            await s.commit()
        r = await _politica(mundo, {"teams.mensaje": "aprobacion"}, usuario="otra-usuaria")
        assert r.status_code == 403
        cab_agente = {
            "X-HermesHQ-Agent-ID": mundo.agent_id,
            "X-HermesHQ-Agent-Token": create_agent_service_token(mundo.agent_id),
        }
        async with httpx.AsyncClient() as c:
            r = await c.put(
                f"{mundo.backend.base}/api/agents/{mundo.agent_id}/aprobacion-central/politica",
                headers=cab_agente,
                json={"politica": {"teams.mensaje": "aprobacion"}},
            )
        assert r.status_code == 401
        assert (await _ver(mundo))["politica_agente"] == {}

    async def test_editar_el_agente_no_cambia_su_politica(self, mundo):
        await _politica(mundo, {"teams.mensaje": "bloqueada"})
        async with httpx.AsyncClient() as c:
            r = await c.put(
                f"{mundo.backend.base}/api/agents/{mundo.agent_id}",
                headers=await _login(mundo.backend.base),
                json={"description": "x", "approval_policy": {}, "approval_central_mode": "desactivada"},
            )
        assert r.status_code in (200, 422), r.text
        ver = await _ver(mundo)
        assert ver["politica_agente"] == {"teams.mensaje": "bloqueada"} and ver["modo"] == "heredar"

    async def test_agente_de_sistema_no_se_concede_permisos_ni_toca_m365(self, mundo):
        async with mundo.maker() as s:
            agente = await s.get(Agent, mundo.agent_id)
            agente.is_system_agent = True
            agente.system_scope = "admin"
            otro = Agent(
                node_id=agente.node_id,
                name="otro",
                slug="otro-agente",
                status="stopped",
                workspace_path=str(mundo.tmp / "otro"),
                integration_configs={"m365_graph": POLITICA},
            )
            s.add(otro)
            await s.commit()
            otro_id = otro.id
        base = f"{mundo.backend.base}/api/internal/control/agents"
        cab = {
            "X-HermesHQ-Agent-ID": mundo.agent_id,
            "X-HermesHQ-Agent-Token": create_agent_service_token(mundo.agent_id),
        }
        amplia = {"correo": {"operaciones": ["mail_list", "mail_read"], "carpetas": ["inbox", "sentitems"]}}
        async with httpx.AsyncClient() as c:
            propio = await c.put(
                f"{base}/{mundo.agent_id}", headers=cab, json={"integration_configs": {"m365_graph": amplia}}
            )
            propio_ts = await c.put(f"{base}/{mundo.agent_id}", headers=cab, json={"enabled_toolsets": ["terminal"]})
            propio_int = await c.put(
                f"{base}/{mundo.agent_id}/integrations/m365_graph",
                headers=cab,
                json={"enabled": True, "config": {"carpetas": "inbox,sentitems"}},
            )
            ajeno = await c.put(f"{base}/{otro_id}", headers=cab, json={"integration_configs": {"m365_graph": amplia}})
            ajeno_int = await c.put(
                f"{base}/{otro_id}/integrations/m365_graph",
                headers=cab,
                json={"enabled": True, "config": {"carpetas": "inbox,sentitems"}},
            )
            compatible = await c.put(f"{base}/{otro_id}", headers=cab, json={"description": "actualizado por operador"})
        assert propio.status_code == 403 and propio.json()["detail"]["codigo"] == "autoconcesion_prohibida"
        assert propio_ts.status_code == 403
        assert propio_int.status_code == 403
        assert (
            ajeno.status_code == 403 and ajeno.json()["detail"]["codigo"] == "configuracion_reservada_al_administrador"
        )
        assert ajeno_int.status_code == 403
        assert compatible.status_code == 200, compatible.text
        async with mundo.maker() as s:
            assert (await s.get(Agent, mundo.agent_id)).integration_configs == {"m365_graph": POLITICA}
            assert (await s.get(Agent, otro_id)).integration_configs == {"m365_graph": POLITICA}
