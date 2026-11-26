from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import requires_database
from test_e2e_correo_migrado import mundo  # noqa: F401

from hermeshq.models.task import Task
from hermeshq.services.execution_lease import ExecutionLeaseStore

pytestmark = [pytest.mark.integration, requires_database]

PEDIDO_REAL = (
    "Crea un dashboard interactivo en HTML con estos datos de tickets de soporte de enero a junio 2026. Adjúntalo aquí, "
    "no lo subas a OneDrive."
)
RUTA = "/me/drive/root:/Pruebas/Dashboard.html:/content"


async def _tarea(m, metadata: dict, minutos_atras: int = 0, running: bool = True):
    async with m.maker() as s:
        t = Task(
            agent_id=m.agent_id,
            prompt="tarea",
            status="running" if running else "completed",
            created_by_user_id=m.admin_id,
            metadata_json={"source": "public_chat", "thread_user_id": m.admin_id, **metadata},
            queued_at=datetime.now(UTC) - timedelta(minutes=minutos_atras),
        )
        s.add(t)
        await s.commit()
        task_id = t.id
    if not running:
        return task_id, None
    lease = await ExecutionLeaseStore(m.maker, owner="runner-e2e").begin(task_id, str(uuid.uuid4()), 1, 600)
    return task_id, lease


async def _subir(m, task_id, lease, ruta=RUTA, metodo="PUT"):
    plugin = m.plugin_para(task_id, lease)
    async with httpx.AsyncClient() as c:
        return await c.post(
            f"{m.backend.base}/api/internal/control/m365/graph",
            headers=plugin._cabeceras_control(),
            json={
                "tarea_id": task_id,
                "metodo": metodo,
                "ruta": ruta,
                "cuerpo_b64": base64.b64encode(b"<html></html>").decode(),
            },
        )


def _subidas_en_graph(m) -> list:
    return [p for p in m.graph.peticiones if p["metodo"] in ("PUT", "POST") and "drive" in p["ruta"]]


class TestSubidaProhibidaEnElBroker:
    async def test_caso_real_el_mensaje_lo_prohibe(self, mundo):  # noqa: F811
        task_id, lease = await _tarea(mundo, {"mensaje_usuario": PEDIDO_REAL, "public_session_id": "s1"})
        r = await _subir(mundo, task_id, lease)
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["motivo"] == "el_usuario_pidio_no_subir"
        assert _subidas_en_graph(mundo) == []

    async def test_trabajo_en_segundo_plano_con_el_pedido_guardado(self, mundo):  # noqa: F811
        task_id, lease = await _tarea(mundo, {"background_job": True, "pedido_del_usuario": PEDIDO_REAL})
        r = await _subir(mundo, task_id, lease)
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["motivo"] == "el_usuario_pidio_no_subir"
        sesion = await _subir(
            mundo, task_id, lease, ruta="/me/drive/root:/Pruebas/D.html:/createUploadSession", metodo="POST"
        )
        assert sesion.status_code == 403, sesion.text
        assert sesion.json()["detail"]["motivo"] == "el_usuario_pidio_no_subir"
        assert _subidas_en_graph(mundo) == []

    async def test_caso_real_1f9c46d0_trabajo_con_tarea_de_origen(self, mundo):  # noqa: F811
        origen, _ = await _tarea(
            mundo,
            {"public_session_id": "s1", "conversation_epoch": 2, "mensaje_usuario": PEDIDO_REAL},
            minutos_atras=2,
            running=False,
        )
        task_id, lease = await _tarea(
            mundo, {"source": "background_job", "background_job": True, "origin_task_id": str(origen)}
        )
        r = await _subir(mundo, task_id, lease)
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["motivo"] == "el_usuario_pidio_no_subir"

    async def test_el_turno_siguiente_de_la_conversacion_tambien(self, mundo):  # noqa: F811
        sesion = {"public_session_id": "s1", "conversation_epoch": 1}
        await _tarea(mundo, {**sesion, "mensaje_usuario": PEDIDO_REAL}, minutos_atras=5, running=False)
        task_id, lease = await _tarea(mundo, {**sesion, "mensaje_usuario": "agrégale un filtro por mes"})
        r = await _subir(mundo, task_id, lease)
        assert r.status_code == 403, r.text
        assert "no lo subas" in r.json()["detail"]["frase"]

    async def test_si_despues_pide_subirlo_no_se_bloquea(self, mundo):  # noqa: F811
        sesion = {"public_session_id": "s1", "conversation_epoch": 1}
        await _tarea(mundo, {**sesion, "mensaje_usuario": PEDIDO_REAL}, minutos_atras=5, running=False)
        task_id, lease = await _tarea(mundo, {**sesion, "mensaje_usuario": "ahora sí, súbelo a OneDrive"})
        r = await _subir(mundo, task_id, lease)
        assert not (r.status_code == 403 and r.json().get("detail", {}).get("motivo") == "el_usuario_pidio_no_subir")

    async def test_otra_conversacion_o_tras_reset_no_hereda(self, mundo):  # noqa: F811
        await _tarea(
            mundo,
            {"public_session_id": "s1", "conversation_epoch": 1, "mensaje_usuario": PEDIDO_REAL},
            minutos_atras=5,
            running=False,
        )
        for metadata in (
            {"public_session_id": "s2", "conversation_epoch": 1, "mensaje_usuario": "guárdalo en Pruebas"},
            {"public_session_id": "s1", "conversation_epoch": 2, "mensaje_usuario": "guárdalo en Pruebas"},
        ):
            task_id, lease = await _tarea(mundo, metadata)
            r = await _subir(mundo, task_id, lease)
            assert not (
                r.status_code == 403 and r.json().get("detail", {}).get("motivo") == "el_usuario_pidio_no_subir"
            )


async def _politica_de_compatibilidad(m):
    from hermeshq.models.agent import Agent

    async with m.maker() as s:
        agente = await s.get(Agent, m.agent_id)
        configs = {k: v for k, v in (agente.integration_configs or {}).items() if k != "m365_graph"}
        agente.integration_configs = {**configs, "sharepoint": {}}
        await s.commit()


class TestDestinoDeLaSubida:
    async def test_subir_a_otra_unidad_se_rechaza(self, mundo):  # noqa: F811
        await _politica_de_compatibilidad(mundo)
        task_id, lease = await _tarea(mundo, {"mensaje_usuario": "guarda el informe en Pruebas"})
        r = await _subir(mundo, task_id, lease, ruta="/drives/b!otra-unidad/root:/Pruebas/Informe.html:/content")
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["motivo"] == "subida_fuera_del_onedrive_propio"

    async def test_el_onedrive_propio_no_cambia(self, mundo):  # noqa: F811
        await _politica_de_compatibilidad(mundo)
        task_id, lease = await _tarea(mundo, {"mensaje_usuario": "guarda el informe en Pruebas"})
        r = await _subir(mundo, task_id, lease)
        detalle = r.json().get("detail") if r.status_code == 403 else {}
        assert not (isinstance(detalle, dict) and detalle.get("motivo") == "subida_fuera_del_onedrive_propio")


async def _habilitar_subida_a_sitios(m, valor=True):
    from hermeshq.models.agent import Agent

    async with m.maker() as s:
        agente = await s.get(Agent, m.agent_id)
        configs = {k: v for k, v in (agente.integration_configs or {}).items() if k != "m365_graph"}
        agente.integration_configs = {**configs, "sharepoint": {"subida_a_sitios": valor}}
        await s.commit()


class TestSubidaABibliotecaDeSitio:
    RUTA_SITIO = "/drives/b!sitio-ti/root:/02 - Proyectos/Informe.html:/content"

    async def test_habilitada_la_subida_a_la_biblioteca_pasa(self, mundo):  # noqa: F811
        await _habilitar_subida_a_sitios(mundo, True)
        task_id, lease = await _tarea(mundo, {"mensaje_usuario": "guarda el informe en la biblioteca de TI"})
        r = await _subir(mundo, task_id, lease, ruta=self.RUTA_SITIO)
        detalle = r.json().get("detail") if r.status_code == 403 else {}
        assert not (isinstance(detalle, dict) and detalle.get("motivo") == "subida_fuera_del_onedrive_propio"), r.text

    async def test_control_negativo_sin_habilitar_o_en_falso_sigue_rechazada(self, mundo):  # noqa: F811
        for valor in (False, "true", 1):
            await _habilitar_subida_a_sitios(mundo, valor)
            task_id, lease = await _tarea(mundo, {"mensaje_usuario": "guarda el informe en la biblioteca de TI"})
            r = await _subir(mundo, task_id, lease, ruta=self.RUTA_SITIO)
            assert r.status_code == 403, (valor, r.text)
            assert r.json()["detail"]["motivo"] == "subida_fuera_del_onedrive_propio"

    async def test_habilitada_igual_se_respeta_el_no_lo_subas(self, mundo):  # noqa: F811
        await _habilitar_subida_a_sitios(mundo, True)
        task_id, lease = await _tarea(mundo, {"mensaje_usuario": PEDIDO_REAL, "public_session_id": "s1"})
        r = await _subir(mundo, task_id, lease, ruta=self.RUTA_SITIO)
        assert r.status_code == 403 and r.json()["detail"]["motivo"] == "el_usuario_pidio_no_subir"
