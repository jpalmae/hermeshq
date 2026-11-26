from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import requires_database, run_queued
from test_referencias_correo_chat import _maker, _mundo

from hermeshq.models.task import Task
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.hermes_runtime import RuntimeExecutionResult

BORRADOR = "**Para:** a@b.cl\n**Asunto:** Prueba\n\n---\n\nEstimado,\n\nTexto.\n\n---\n\n_Sin firma_"
VERSION_DEL_MODELO = "Aquí está la vista previa:\n\n> Estimado,\n>\n> Texto.\n\n¿Lo envío?"


def _mensajes():
    resultado = {"success": False, "requiere_aprobacion": True, "borrador": BORRADOR, "vista_previa": {}}
    return [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "ms365_mail_send"}}]},
        {"role": "tool", "name": "ms365_mail_send", "tool_call_id": "1", "content": json.dumps(resultado)},
        {"role": "assistant", "content": VERSION_DEL_MODELO},
    ]


@pytest.mark.integration
@requires_database
class TestEventoConBorradorCanonico:
    async def test_el_chat_recibe_el_borrador_del_servidor_y_no_la_version_del_modelo(self, db_engine):
        maker = _maker(db_engine)
        e = await _mundo(maker)
        async with maker() as db:
            tarea = Task(
                agent_id=e["agente"].id,
                title="Public chat message",
                prompt="envía un correo a a@b.cl",
                status="queued",
                queued_at=datetime.now(UTC) - timedelta(minutes=1),
                metadata_json={
                    "conversation": True,
                    "source": "public_chat",
                    "public_session_id": e["s1"].id,
                    "thread_user_id": e["ana"].id,
                    "public_chat_api_key_id": e["k1"].id,
                    "public_chat_bound_user_id": e["ana"].id,
                },
            )
            db.add(tarea)
            await db.commit()
        resultado = RuntimeExecutionResult(
            final_response=VERSION_DEL_MODELO,
            messages=_mensajes(),
            tool_calls=[{"name": "ms365_mail_send"}],
            tokens_used=10,
            iterations=2,
            engine="stub",
            response_attachments=[],
            usage={"turn_exit_reason": "text_response(finish_reason=stop)", "api_calls": 2},
        )
        runtime = MagicMock()
        runtime.execute = AsyncMock(return_value=resultado)
        broker = MagicMock(publish=AsyncMock())
        supervisor = AgentSupervisor(maker, broker, runtime, secret_vault=None)
        supervisor.submit_task = AsyncMock()
        await run_queued(supervisor, tarea.id)
        async with maker() as db:
            guardada = (await db.get(Task, tarea.id)).response
        eventos = [c.args[0] for c in broker.publish.call_args_list if c.args[0].get("type") == "task.completed"]
        assert len(eventos) == 1
        assert BORRADOR in guardada
        assert eventos[0]["response"] == guardada
        assert "> Estimado" not in eventos[0]["response"]
