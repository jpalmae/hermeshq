import pytest
from conftest import requires_database
from test_execution_lease import _seed_task
from test_execution_lease_e2e import _maker

from hermeshq.models.task import Task
from hermeshq.services.archivos_conversacion import sesion_recien_reiniciada, sesion_sin_historial

pytestmark = [pytest.mark.integration, requires_database]


class TestSesionReiniciadaEnBaseDeDatos:
    async def test_solo_la_ultima_tarea_de_la_sesion_decide(self, db_engine):
        maker = _maker(db_engine)
        agent_id, _ = await _seed_task(maker)
        async with maker() as session:
            assert await sesion_recien_reiniciada(session, agent_id, "ses-r1") is False
            assert await sesion_sin_historial(session, agent_id, "ses-r1") is True
        async with maker() as session:
            session.add(
                Task(
                    agent_id=agent_id, prompt="hola", status="completed", metadata_json={"public_session_id": "ses-r1"}
                )
            )
            await session.commit()
        async with maker() as session:
            assert await sesion_recien_reiniciada(session, agent_id, "ses-r1") is False
            assert await sesion_sin_historial(session, agent_id, "ses-r1") is False
        async with maker() as session:
            session.add(
                Task(
                    agent_id=agent_id,
                    prompt="/reset",
                    status="completed",
                    metadata_json={"reset": True, "public_session_id": "ses-r1"},
                )
            )
            await session.commit()
        async with maker() as session:
            assert await sesion_recien_reiniciada(session, agent_id, "ses-r1") is True
            assert await sesion_sin_historial(session, agent_id, "ses-r1") is False, (
                "el /reset es una tarea de la sesión"
            )
            assert await sesion_recien_reiniciada(session, agent_id, "ses-r2") is False
        async with maker() as session:
            session.add(
                Task(agent_id=agent_id, prompt="ok", status="completed", metadata_json={"public_session_id": "ses-r1"})
            )
            await session.commit()
        async with maker() as session:
            assert await sesion_recien_reiniciada(session, agent_id, "ses-r1") is False
