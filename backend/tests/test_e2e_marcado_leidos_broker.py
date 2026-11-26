from __future__ import annotations

import uuid

import httpx
import pytest
from conftest import requires_database
from test_e2e_correo_migrado import mundo  # noqa: F401

from hermeshq.models.task import Task
from hermeshq.services.execution_lease import ExecutionLeaseStore
from hermeshq.services.marcado_leidos import CLAVE_METADATA

pytestmark = [pytest.mark.integration, requires_database]


async def _tarea_programada(m, autorizado: bool, prompt: str):
    async with m.maker() as s:
        t = Task(
            agent_id=m.agent_id,
            prompt=prompt,
            status="running",
            created_by_user_id=m.admin_id,
            metadata_json={"scheduled": True, "thread_user_id": m.admin_id, CLAVE_METADATA: autorizado},
        )
        s.add(t)
        await s.commit()
        task_id = t.id
    lease = await ExecutionLeaseStore(m.maker, owner="runner-e2e").begin(task_id, str(uuid.uuid4()), 1, 600)
    return task_id, lease


async def _patch_directo(m, plugin, task_id):
    async with httpx.AsyncClient() as c:
        return await c.post(
            f"{m.backend.base}/api/internal/control/m365/graph",
            headers=plugin._cabeceras_control(),
            json={
                "tarea_id": task_id,
                "metodo": "PATCH",
                "ruta": "/me/messages/orig-00001",
                "cuerpo_json": {"isRead": True},
            },
        )


class TestBrokerMarcadoAutomatico:
    async def test_cron_sin_declaracion_recibe_403_y_graph_no_ve_patch(self, mundo):  # noqa: F811
        task_id, lease = await _tarea_programada(mundo, False, "Resumen de no leídos")
        plugin = mundo.plugin_para(task_id, lease)
        r = await _patch_directo(mundo, plugin, task_id)
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["motivo"] == "marcar_leidos_no_declarado_en_tarea_automatica"
        assert mundo.graph.marcados == []

    async def test_cron_con_declaracion_marca(self, mundo):  # noqa: F811
        task_id, lease = await _tarea_programada(mundo, True, "Resumen y marca como leídos los correos")
        plugin = mundo.plugin_para(task_id, lease)
        r = await _patch_directo(mundo, plugin, task_id)
        assert r.status_code == 200, r.text
        assert mundo.graph.marcados == [("orig-00001", {"isRead": True})]
