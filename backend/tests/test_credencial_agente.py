import time
import uuid

import pytest
from conftest import requires_database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_execution_lease import _seed_task

from hermeshq.config import get_settings
from hermeshq.core.security import create_agent_service_token
from hermeshq.services import credencial_agente as ca
from hermeshq.services.execution_lease import ExecutionLeaseStore


class TestFormato:
    def test_emitido_se_decodifica_con_sus_datos(self):
        token = ca.emitir("ag", "t1", "x1", 3, 600)
        carga = ca.decodificar(token)
        assert token.startswith("v2.") and (carga["a"], carga["t"], carga["x"], carga["g"]) == ("ag", "t1", "x1", 3)

    def test_alterado_caducado_o_estatico_no_se_decodifica(self, monkeypatch):
        token = ca.emitir("ag", "t1", "x1", 3, 600)
        cuerpo, firma = token[3:].split(".")
        otro = ca.emitir("intruso", "t1", "x1", 3, 600)[3:].split(".")[0]
        assert ca.decodificar(f"v2.{otro}.{firma}") is None
        assert ca.decodificar(token[:-2] + "AA") is None
        assert ca.decodificar(create_agent_service_token("ag")) is None
        monkeypatch.setattr(time, "time", lambda: 10**10)
        assert ca.decodificar(token) is None

    def test_la_clave_depende_del_secreto_del_servidor(self, monkeypatch):
        token = ca.emitir("ag", "t1", "x1", 1, 600)
        monkeypatch.setattr(get_settings(), "jwt_secret", "otro-secreto-" + "y" * 40)
        assert ca.decodificar(token) is None


@pytest.mark.integration
@requires_database
class TestValidacionContraElIntento:
    async def _intento(self, db_engine):
        engine, _ = db_engine
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        agent_id, task_id = await _seed_task(maker)
        store = ExecutionLeaseStore(maker, owner="prueba-credencial")
        lease = await store.begin(task_id, str(uuid.uuid4()), 1, 600)
        return maker, store, agent_id, task_id, lease

    async def test_intento_vivo_vale_y_terminado_no(self, db_engine):
        maker, store, agent_id, task_id, lease = await self._intento(db_engine)
        token = ca.emitir(agent_id, task_id, lease.attempt_id, lease.generation, 600)
        async with maker() as db:
            assert await ca.credencial_valida(db, agent_id, token)
        await store.close_quietly(lease, "completed", end_reason="completed")
        async with maker() as db:
            assert not await ca.credencial_valida(db, agent_id, token), "al terminar la tarea el token ya no sirve"

    async def test_otro_agente_otra_generacion_o_intento_declarado_distinto(self, db_engine):
        maker, _store, agent_id, task_id, lease = await self._intento(db_engine)
        async with maker() as db:
            assert not await ca.credencial_valida(
                db, "otro-agente", ca.emitir(agent_id, task_id, lease.attempt_id, lease.generation, 600)
            )
            assert not await ca.credencial_valida(
                db, agent_id, ca.emitir(agent_id, task_id, lease.attempt_id, lease.generation + 1, 600)
            )
            assert not await ca.credencial_valida(
                db, agent_id, ca.emitir(agent_id, "otra-tarea", lease.attempt_id, lease.generation, 600)
            )
            token = ca.emitir(agent_id, task_id, lease.attempt_id, lease.generation, 600)
            assert not await ca.credencial_valida(
                db, agent_id, token, cabeceras={"X-HermesHQ-Attempt-ID": "intento-ajeno"}
            )
            assert await ca.credencial_valida(
                db, agent_id, token, cabeceras={"X-HermesHQ-Attempt-ID": lease.attempt_id}
            )

    async def test_estatico_segun_el_modo(self, db_engine, monkeypatch):
        maker, _store, agent_id, *_ = await self._intento(db_engine)
        estatico = create_agent_service_token(agent_id)
        async with maker() as db:
            for modo, esperado in (("off", True), ("dual", True), ("strict", False)):
                monkeypatch.setattr(get_settings(), "agent_token_mode", modo)
                assert await ca.credencial_valida(db, agent_id, estatico) is esperado, modo
            assert not await ca.credencial_valida(db, agent_id, "falso")
            assert not await ca.credencial_valida(db, agent_id, create_agent_service_token(agent_id, 2))

    async def test_cada_uso_del_estatico_queda_contado_por_origen(self, db_engine, monkeypatch):
        maker, _store, agent_id, task_id, lease = await self._intento(db_engine)
        estatico = create_agent_service_token(agent_id)
        antes = ca.usos_del_estatico()
        async with maker() as db:
            monkeypatch.setattr(get_settings(), "agent_token_mode", "dual")
            await ca.credencial_valida(db, agent_id, estatico, origen="resolve_channel_user")
            await ca.credencial_valida(db, agent_id, estatico, origen="resolve_channel_user")
            await ca.credencial_valida(
                db, agent_id, ca.emitir(agent_id, task_id, lease.attempt_id, lease.generation, 600), origen="comms"
            )
            monkeypatch.setattr(get_settings(), "agent_token_mode", "strict")
            await ca.credencial_valida(db, agent_id, estatico, origen="internal_agents")
            await ca.credencial_valida(db, agent_id, "falso", origen="internal_agents")
        despues = ca.usos_del_estatico()

        def delta(clave):
            return despues.get(clave, 0) - antes.get(clave, 0)

        assert delta(("resolve_channel_user", "aceptado")) == 2
        assert delta(("internal_agents", "rechazado")) == 1, "el falso no cuenta como uso del estático"
        assert delta(("comms", "aceptado")) == 0, "el v2 no cuenta como uso del estático"


class TestCompatibilidadStrict:
    async def test_el_entorno_del_gateway_y_de_la_terminal_lleva_el_estatico(self):
        from pathlib import Path

        fuente = (
            Path(ca.__file__).resolve().parents[1] / "services" / "hermes_installation" / "manager.py"
        ).read_text()
        assert (
            'env["HERMESHQ_AGENT_TOKEN"] = create_agent_service_token(agent.id, agent.service_token_version or 1)'
            in fuente
        ), "si esto cambia, el gateway y la terminal ya no dependen del estático: revisar el procedimiento de strict"
