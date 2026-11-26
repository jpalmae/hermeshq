from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import requires_database, run_queued
from test_falsos_exitos import _avisos, _maker, _preparar, _supervisor, _tareas

from hermeshq.services import auto_background
from hermeshq.services.confirmacion import confirma_una_aprobacion, es_confirmacion
from hermeshq.services.hermes_runtime import RuntimeExecutionError

TIMEOUT = "Hermes runtime exceeded the task timeout of 600s"


def _tarea(mensaje):
    return SimpleNamespace(prompt=mensaje, metadata_json={"mensaje_usuario": mensaje})


class TestEsConfirmacion:
    @pytest.mark.parametrize(
        "mensaje", ["sí", "Si", "ok", "dale, envíalo", "sí por favor", "eliminar QAM8YA", "confirmo"]
    )
    def test_confirmaciones(self, mensaje):
        assert es_confirmacion(_tarea(mensaje))

    @pytest.mark.parametrize(
        "mensaje", ["sí, pero cambia el asunto a Reunión", "si vamos con el informe", "hazme un informe", "no"]
    )
    def test_no_confirmaciones(self, mensaje):
        assert not es_confirmacion(_tarea(mensaje))

    def test_solo_una_confirmacion_de_aprobacion_queda_fuera_del_segundo_plano(self):
        vista = [SimpleNamespace(role="assistant", content="**Para:** yo@x.cl\n**Asunto:** Hola\n\n¿Lo envío?")]
        propuesta = [
            SimpleNamespace(role="assistant", content="Propongo 7 capítulos. ¿Avanzo con el informe completo?")
        ]
        assert confirma_una_aprobacion(_tarea("sí"), vista)
        assert not confirma_una_aprobacion(_tarea("sí"), propuesta)
        assert not confirma_una_aprobacion(_tarea("hazme el informe"), vista)


@pytest.mark.integration
@requires_database
class TestSupervisor:
    CHAT = {"source": "public_chat", "thread_user_id": "@", "public_session_id": "s1", "respuesta_tardia": True}

    async def test_caso_real_un_si_cortado_avisa_una_vez_y_no_pasa_a_segundo_plano(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _preparar(maker, {**self.CHAT, "mensaje_usuario": "sí"})
        vista = [SimpleNamespace(role="assistant", content="Vista previa del reenvío … ¿Lo envío?")]
        supervisor = _supervisor(maker, error=RuntimeExecutionError(TIMEOUT))
        with (
            patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)),
            patch.object(auto_background, "_load_recent_messages", AsyncMock(return_value=vista)),
        ):
            await run_queued(supervisor, task_id)
        assert [t for t in await _tareas(maker, agent_id) if (t.metadata_json or {}).get("origin_task_id")] == []
        avisos = [a.body for a in await _avisos(maker, agent_id)]
        assert len(avisos) == 1, avisos
        assert "Escribe «sí» de nuevo" in avisos[0]

    async def test_un_pedido_cortado_que_sigue_en_segundo_plano_no_recibe_tambien_el_fallo(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _preparar(maker, {**self.CHAT, "mensaje_usuario": "Genera el informe por rol"})
        supervisor = _supervisor(maker, error=RuntimeExecutionError(TIMEOUT))
        with patch.object(auto_background, "can_access_agent", AsyncMock(return_value=True)):
            await run_queued(supervisor, task_id)
        nuevas = [t for t in await _tareas(maker, agent_id) if (t.metadata_json or {}).get("origin_task_id")]
        assert len(nuevas) == 1
        avisos = [a.body for a in await _avisos(maker, agent_id)]
        assert not any(a.startswith("No pude completar") for a in avisos), avisos
        assert any(a.startswith("⏳") for a in avisos), avisos
