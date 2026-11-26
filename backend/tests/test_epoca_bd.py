import pytest
from conftest import requires_database
from test_execution_lease import _seed_task
from test_execution_lease_e2e import _maker

from hermeshq.models.task import Task
from hermeshq.services.public_chat_service import _epoca_actual

pytestmark = [pytest.mark.integration, requires_database]


class TestEpocaEnBaseDeDatos:
    async def test_la_epoca_cuenta_los_reinicios_de_cada_sesion_publica(self, db_engine):
        maker = _maker(db_engine)
        agent_id, _ = await _seed_task(maker)
        async with maker() as session:
            assert await _epoca_actual(session, "sess-1") == 0
        async with maker() as session:
            session.add(
                Task(
                    agent_id=agent_id,
                    prompt="/reset",
                    status="completed",
                    metadata_json={"reset": True, "public_session_id": "sess-1"},
                )
            )
            session.add(
                Task(
                    agent_id=agent_id, prompt="hola", status="completed", metadata_json={"public_session_id": "sess-1"}
                )
            )
            await session.commit()
        async with maker() as session:
            assert await _epoca_actual(session, "sess-1") == 1
            assert await _epoca_actual(session, "sess-2") == 0
