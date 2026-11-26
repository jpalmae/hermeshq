from __future__ import annotations

import signal
import uuid

import pytest
from conftest import requires_database
from test_e2e_http_recuperacion import (
    _api,
    _attempts_with_heartbeat,
    _final_status,
    _login,
    _requests_for,
    _until,
    stack,  # noqa: F401
)

from hermeshq.models.task import Task

pytestmark = [pytest.mark.integration, requires_database]


async def _tarea_en_curso(stack):  # noqa: F811
    stack["handler"].delay = 20.0
    a = stack["spawn"]("instancia-a")
    await a.wait_ready()
    headers = await _login(a.base)
    marca = f"marca-{uuid.uuid4().hex[:8]}"
    tarea = (
        await _api(
            "POST", a.base, "/tasks", headers, json={"agent_id": stack["agent_id"], "prompt": f"Responde {marca}"}
        )
    )["id"]
    await _until(lambda: _attempts_with_heartbeat(stack["maker"], tarea, 1), 60, "latidos")
    return a, headers, tarea, marca


class TestApagado:
    async def test_sigterm_deja_la_tarea_interrumpida_y_reintentable(self, stack):  # noqa: F811
        a, headers, tarea, marca = await _tarea_en_curso(stack)
        a.process.send_signal(signal.SIGTERM)
        a.process.wait(timeout=30)
        async with stack["maker"]() as s:
            t = await s.get(Task, tarea)
        assert t.status == "failed", t.status
        assert t.metadata_json["recovery"] == {
            "estado": "interrumpida",
            "motivo": "apagado_del_servicio",
            "reintentable": True,
        }
        assert "reinicio del servicio" in t.error_message
        assert _requests_for(stack["handler"], marca) <= 1

    async def test_cancelacion_del_usuario_sigue_siendo_cancelled(self, stack):  # noqa: F811
        a, headers, tarea, marca = await _tarea_en_curso(stack)
        await _api("POST", a.base, f"/tasks/{tarea}/cancel", headers)
        final = await _until(lambda: _final_status(a.base, headers, tarea), 30, "cancelación")
        assert final["status"] == "cancelled"
        async with stack["maker"]() as s:
            t = await s.get(Task, tarea)
        assert "recovery" not in (t.metadata_json or {})
