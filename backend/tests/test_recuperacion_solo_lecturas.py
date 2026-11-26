from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import requires_database
from test_efectos_herramientas import LECTURAS_DEL_CRON
from test_execution_lease import _seed_task
from test_execution_lease_e2e import _maker, _orphan, _task

from hermeshq.models.base import utcnow
from hermeshq.models.task import Task
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.execution_lease import ExecutionLeaseStore, clasificar_interrupcion, mensaje_de_fallo

pytestmark = [pytest.mark.integration, requires_database]


async def _intento_perdido(maker, task_id: str, agent_id: str, nombres: list[str], tool_events: int):
    store, lease = await _orphan(maker, task_id, "host-reiniciado", tool_events=tool_events, ttl=1)
    for nombre in nombres:
        await store.registrar_inicio_herramienta(lease, agent_id, nombre)
    return lease


async def _clase(maker, task_id: str) -> str:
    async with maker() as session:
        task = await session.get(Task, task_id)
        clase, _ = await clasificar_interrupcion(session, task)
        await session.commit()
    return clase


async def _vencer_claim(maker, task_id: str) -> None:
    async with maker() as session:
        task = await session.get(Task, task_id)
        task.status = "running"
        task.attempt_count = 1
        task.claimed_by = "instancia-caida"
        task.claim_token = "claim-viejo"
        task.lease_expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()


class TestClasificacionSegunHerramientasIniciadas:
    async def test_solo_lecturas_queda_interrumpida(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _seed_task(maker)
        await _intento_perdido(maker, task_id, agent_id, LECTURAS_DEL_CRON, 17)
        assert await _clase(maker, task_id) == "interrumpida"

    @pytest.mark.parametrize(
        "con_efecto", ["sharepoint_upload_file", "ms365_mail_send", "hq_trabajo_largo", "delegate_task", "nueva"]
    )
    async def test_una_herramienta_con_efectos_deja_el_intento_incierto(self, db_engine, con_efecto):
        maker = _maker(db_engine)
        agent_id, task_id = await _seed_task(maker)
        await _intento_perdido(maker, task_id, agent_id, LECTURAS_DEL_CRON[:-1] + [con_efecto], 17)
        assert await _clase(maker, task_id) == "resultado_incierto"

    async def test_herramienta_iniciada_y_no_terminada_cuenta(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _seed_task(maker)
        await _intento_perdido(maker, task_id, agent_id, ["ms365_mail_list", "teams_send_to_person"], 1)
        assert await _clase(maker, task_id) == "resultado_incierto"

    async def test_un_inicio_sin_registrar_deja_el_intento_incierto(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _seed_task(maker)
        await _intento_perdido(maker, task_id, agent_id, LECTURAS_DEL_CRON[:16], 17)
        assert await _clase(maker, task_id) == "resultado_incierto"

    async def test_los_inicios_de_otro_intento_no_cuentan(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _seed_task(maker)
        _, lease = await _orphan(maker, task_id, "host-reiniciado", tool_events=1, ttl=1)
        otro = ExecutionLeaseStore(maker, owner="otro")
        ajeno = type(lease)(**{**lease.__dict__, "attempt_id": "otro-intento"})
        await otro.registrar_inicio_herramienta(ajeno, agent_id, "ms365_mail_list")
        assert await _clase(maker, task_id) == "resultado_incierto"


class TestRecuperacionDeLaColaDurable:
    def _supervisor(self, maker) -> AgentSupervisor:
        return AgentSupervisor(maker, MagicMock(publish=AsyncMock()), MagicMock(), secret_vault=None)

    async def test_un_corte_que_solo_leia_vuelve_a_la_cola(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _seed_task(maker)
        await _intento_perdido(maker, task_id, agent_id, LECTURAS_DEL_CRON, 17)
        await _vencer_claim(maker, task_id)
        assert await self._supervisor(maker)._recover_expired_tasks() == 1
        task = await _task(maker, task_id)
        assert task.status == "queued"
        assert task.claim_token is None

    async def test_un_corte_tras_subir_un_archivo_no_se_repite(self, db_engine):
        maker = _maker(db_engine)
        agent_id, task_id = await _seed_task(maker)
        await _intento_perdido(maker, task_id, agent_id, ["sharepoint_upload_file"], 1)
        await _vencer_claim(maker, task_id)
        await self._supervisor(maker)._recover_expired_tasks()
        task = await _task(maker, task_id)
        assert task.status == "failed"
        assert task.error_message == mensaje_de_fallo("resultado_incierto")
        assert task.metadata_json["recovery"]["estado"] == "resultado_incierto"
