from __future__ import annotations

import asyncio
import base64
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from conftest import requires_database
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.security import create_agent_service_token
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.node import Node
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.routers import m365_broker
from hermeshq.services import graph_proxy as gp
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.execution_lease import (
    ExecutionLeaseStore,
    ReconciliationRequiredError,
    clasificar_interrupcion,
)

pytestmark = [pytest.mark.integration, requires_database]

UNIDAD = "drv-sintetico-1"
RUTA = f"/drives/{UNIDAD}/root:/Salidas/HermesHQ/informe.txt:/content"
CONTENIDO = base64.b64encode(b"informe sintetico").decode()


class _Graph:
    def __init__(self):
        self.llamadas: list[tuple] = []
        self.fallar = False

    def __call__(self, metodo, url, token, cuerpo, tipo, limite):
        self.llamadas.append((metodo, url))
        if self.fallar:
            raise TimeoutError("Graph no respondió tras recibir la petición")
        return 201, b'{"id": "archivo-nuevo"}'


@pytest.fixture
def graph(monkeypatch):
    red = _Graph()
    monkeypatch.setattr(gp, "_llamar", red)
    monkeypatch.setenv("HERMESHQ_ENTORNO", "aislado")
    monkeypatch.setenv("HERMESHQ_GRAPH_TOKEN_SIMULADO", "token-graph-sintetico")
    return red


@pytest.fixture
async def mundo(db_engine):
    engine, _ = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    sufijo = uuid.uuid4().hex[:6]
    async with maker() as session:
        user = User(username=f"u-{sufijo}", display_name="Usuaria", password_hash="x")
        node = Node(name=f"n-{sufijo}", hostname="localhost")
        session.add_all([user, node])
        await session.flush()
        agent = Agent(
            node_id=node.id,
            name=f"a-{sufijo}",
            slug=f"a-{sufijo}",
            workspace_path=f"/tmp/ws-{sufijo}",
            integration_configs={
                "m365_graph": {
                    "unidades": [UNIDAD],
                    "carpetas_escritura": ["Salidas/HermesHQ"],
                    "operaciones": ["upload", "list"],
                }
            },
        )
        session.add(agent)
        await session.flush()
        session.add(AgentAssignment(agent_id=agent.id, user_id=user.id))
        task = Task(agent_id=agent.id, prompt="sube el informe", status="running", created_by_user_id=user.id)
        session.add(task)
        await session.commit()
        ids = {"agent": agent.id, "user": user.id, "task": task.id}

    app = FastAPI()
    app.include_router(m365_broker.router)

    async def _db():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db_session] = _db
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hq")
    try:
        yield {"maker": maker, "client": client, **ids}
    finally:
        await client.aclose()


def _headers(m, lease=None, **over):
    h = {"X-HermesHQ-Agent-ID": m["agent"], "X-HermesHQ-Agent-Token": create_agent_service_token(m["agent"])}
    if lease is not None:
        h["X-HermesHQ-Attempt-ID"] = lease.attempt_id
        h["X-HermesHQ-Lease-Generation"] = str(lease.generation)
    h.update(over)
    return h


async def _subir(m, lease=None, **over):
    return await m["client"].post(
        "/internal/control/m365/graph",
        headers=_headers(m, lease, **over),
        json={"tarea_id": m["task"], "metodo": "PUT", "ruta": RUTA, "cuerpo_b64": CONTENIDO},
    )


async def _registros(maker, task_id):
    async with maker() as session:
        return (
            (
                await session.execute(
                    text("SELECT * FROM external_action_records WHERE task_id = :t ORDER BY created_at"),
                    {"t": task_id},
                )
            )
            .mappings()
            .all()
        )


async def _vencer(maker, attempt_id):
    async with maker() as session:
        await session.execute(
            text(
                "UPDATE task_execution_attempts SET lease_expires_at = now() - interval '1 second' "
                "WHERE attempt_id = :a"
            ),
            {"a": attempt_id},
        )
        await session.commit()


class TestFencingOfExternalActions:
    async def test_live_attempt_executes_and_records_confirmed_action(self, mundo, graph):
        store = ExecutionLeaseStore(mundo["maker"], owner="instancia-A")
        lease = await store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)
        r = await _subir(mundo, lease)
        assert r.status_code == 200, r.text
        assert len(graph.llamadas) == 1
        rows = await _registros(mundo["maker"], mundo["task"])
        assert [(x["state"], x["attempt_id"], x["lease_generation"]) for x in rows] == [
            ("confirmada", lease.attempt_id, lease.generation)
        ]

    async def test_request_without_attempt_identity_is_rejected_before_graph(self, mundo, graph):
        r = await _subir(mundo)
        assert r.status_code == 403
        assert r.json()["detail"]["motivo"] == "intento_no_identificado"
        assert graph.llamadas == []

    async def test_obsolete_attempt_cannot_start_action_even_if_task_is_running_again(self, mundo, graph):
        old_store = ExecutionLeaseStore(mundo["maker"], owner="instancia-caida")
        old = await old_store.begin(mundo["task"], str(uuid.uuid4()), 1, 1)
        await _vencer(mundo["maker"], old.attempt_id)
        async with mundo["maker"]() as session:
            task = await session.get(Task, mundo["task"])
            assert (await clasificar_interrupcion(session, task))[0] == "interrumpida"
            task.status = "running"
            await session.commit()
        new_store = ExecutionLeaseStore(mundo["maker"], owner="instancia-B")
        new = await new_store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)

        stale = await _subir(mundo, old)
        assert stale.status_code == 403
        assert stale.json()["detail"]["motivo"] == "intento_no_vigente"
        forged_generation = await _subir(mundo, new, **{"X-HermesHQ-Lease-Generation": str(new.generation - 1)})
        assert forged_generation.status_code == 403
        assert graph.llamadas == []

        ok = await _subir(mundo, new)
        assert ok.status_code == 200
        assert len(graph.llamadas) == 1
        rows = await _registros(mundo["maker"], mundo["task"])
        assert [x["attempt_id"] for x in rows] == [new.attempt_id]

    async def test_expired_lease_of_running_attempt_cannot_act(self, mundo, graph):
        store = ExecutionLeaseStore(mundo["maker"], owner="instancia-A")
        lease = await store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)
        await _vencer(mundo["maker"], lease.attempt_id)
        r = await _subir(mundo, lease)
        assert r.status_code == 403
        assert graph.llamadas == []


class TestUncertainActionsRequireReconciliation:
    async def test_uncertain_action_blocks_retry_until_reconciled(self, mundo, graph):
        maker = mundo["maker"]
        store = ExecutionLeaseStore(maker, owner="instancia-A")
        lease = await store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)
        graph.fallar = True
        r = await _subir(mundo, lease)
        assert r.status_code == 502
        assert r.json()["detail"]["motivo"] == "resultado_incierto"
        assert len(graph.llamadas) == 1
        graph.fallar = False

        again = await _subir(mundo, lease)
        assert again.status_code == 409
        assert again.json()["detail"]["motivo"] == "conciliacion_requerida"
        assert len(graph.llamadas) == 1

        await _vencer(maker, lease.attempt_id)
        async with maker() as session:
            task = await session.get(Task, mundo["task"])
            task.status = "running"
            task.attempt_count = 1
            task.claim_token = "claim-caido"
            task.lease_expires_at = None
            await session.commit()
        supervisor = AgentSupervisor(maker, MagicMock(publish=AsyncMock()), MagicMock(), secret_vault=None)
        assert await supervisor._recover_expired_tasks() == 1
        async with maker() as session:
            task = await session.get(Task, mundo["task"])
            recovery = task.metadata_json["recovery"]
            assert task.status == "failed"
            assert recovery["requiere_conciliacion"] is True
            assert recovery["reintentable"] is False
            assert recovery["acciones"][0]["estado"] == "iniciada"
            task.status = "queued"
            await session.commit()

        with pytest.raises(ReconciliationRequiredError):
            await store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)

        from hermeshq.services.external_actions import reconcile_external_action

        action_id = (await _registros(maker, mundo["task"]))[0]["id"]
        async with maker() as session:
            assert await reconcile_external_action(session, mundo["task"], action_id, False, mundo["user"], "no llegó")
        retry = await store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)
        ok = await _subir(mundo, retry)
        assert ok.status_code == 200
        assert len(graph.llamadas) == 2
        states = [x["state"] for x in await _registros(maker, mundo["task"])]
        assert states == ["conciliada_no_ejecutada", "confirmada"]

    async def test_same_action_is_not_executed_twice_by_retries(self, mundo, graph):
        maker = mundo["maker"]
        store = ExecutionLeaseStore(maker, owner="instancia-A")
        first = await store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)
        assert (await _subir(mundo, first)).status_code == 200
        await store.transition(first, "running", "failed", end_reason="runtime_error")
        second = await store.begin(mundo["task"], str(uuid.uuid4()), 2, 600)
        dup = await _subir(mundo, second)
        assert dup.status_code == 409
        assert dup.json()["detail"]["motivo"] == "accion_ya_ejecutada"
        assert len(graph.llamadas) == 1

    async def test_concurrent_identical_actions_execute_once(self, mundo, graph):
        maker = mundo["maker"]
        store = ExecutionLeaseStore(maker, owner="instancia-A")
        lease = await store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)
        results = await asyncio.gather(*(_subir(mundo, lease) for _ in range(5)))
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 409, 409, 409, 409]
        assert len(graph.llamadas) == 1


class TestTokenIssuanceIsFenced:
    @pytest.fixture(autouse=True)
    def _transicion(self, monkeypatch):
        from hermeshq.config import get_settings

        monkeypatch.setattr(get_settings(), "m365_agent_token_enabled", True)
        monkeypatch.setattr(get_settings(), "approval_central_enabled", False)

    async def test_retirada_por_defecto_responde_410(self, mundo, monkeypatch):
        from hermeshq.config import get_settings

        monkeypatch.setattr(get_settings(), "m365_agent_token_enabled", False)
        store = ExecutionLeaseStore(mundo["maker"], owner="instancia-A")
        lease = await store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)
        r = await self._token(mundo, lease)
        assert r.status_code == 410 and r.json()["detail"]["motivo"] == "entrega_de_tokens_retirada"

    async def _token(self, mundo, lease=None):
        from hermeshq.routers import m365 as m365_router

        app = FastAPI()
        app.include_router(m365_router.router)
        app.state.secret_vault = None

        async def _db():
            async with mundo["maker"]() as session:
                yield session

        app.dependency_overrides[get_db_session] = _db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hq") as client:
            return await client.get(
                "/m365/agent-token", params={"user_id": mundo["user"]}, headers=_headers(mundo, lease)
            )

    async def test_obsolete_or_missing_attempt_gets_no_m365_token(self, mundo):
        store = ExecutionLeaseStore(mundo["maker"], owner="instancia-A")
        lease = await store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)
        missing = await self._token(mundo)
        assert missing.status_code == 403
        assert missing.json()["detail"]["motivo"] == "intento_no_identificado"
        await _vencer(mundo["maker"], lease.attempt_id)
        expired = await self._token(mundo, lease)
        assert expired.status_code == 403
        assert expired.json()["detail"]["motivo"] == "intento_no_vigente"

    async def test_live_attempt_passes_the_fence(self, mundo):
        store = ExecutionLeaseStore(mundo["maker"], owner="instancia-A")
        lease = await store.begin(mundo["task"], str(uuid.uuid4()), 1, 600)
        try:
            r = await self._token(mundo, lease)
            detail = r.json().get("detail")
        except Exception as exc:  # noqa: BLE001
            detail = repr(exc)
        assert not (
            isinstance(detail, dict) and detail.get("motivo") in ("intento_no_vigente", "intento_no_identificado")
        )
