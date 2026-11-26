from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.models.task import Task
from hermeshq.services.execution_lease import (
    AttemptConflictError,
    ExecutionLeaseStore,
    LeaseLostError,
    LeaseNotAcquiredError,
)

pytestmark = [pytest.mark.integration, requires_database]


async def _seed_task(maker, workspace: str = "/tmp/ws-lease") -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    async with maker() as session:
        node = Node(name=f"nodo-{suffix}", hostname="localhost")
        session.add(node)
        await session.flush()
        agent = Agent(node_id=node.id, name=f"agente-{suffix}", slug=f"agente-{suffix}", workspace_path=workspace)
        session.add(agent)
        await session.flush()
        task = Task(agent_id=agent.id, prompt="hola", status="running")
        session.add(task)
        await session.commit()
        return agent.id, task.id


async def _attempt_row(maker, attempt_id: str):
    async with maker() as session:
        return (
            (
                await session.execute(
                    text("SELECT * FROM task_execution_attempts WHERE attempt_id = :a"), {"a": attempt_id}
                )
            )
            .mappings()
            .one()
        )


class TestExecutionLease:
    async def test_two_owners_cannot_acquire_the_same_attempt(self, db_engine):
        engine, _ = db_engine
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        _, task_id = await _seed_task(maker)
        store = ExecutionLeaseStore(maker, owner="arbitro")
        attempt_id = await store.create_attempt(task_id, str(uuid.uuid4()), 1)

        owners = [ExecutionLeaseStore(maker, owner=f"propietario-{i}") for i in range(8)]
        barrier = asyncio.Barrier(len(owners))

        async def contend(candidate: ExecutionLeaseStore):
            await barrier.wait()
            try:
                return await candidate.acquire(attempt_id, 600)
            except LeaseNotAcquiredError as exc:
                return exc

        results = await asyncio.gather(*(contend(o) for o in owners))
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, LeaseNotAcquiredError)]
        assert len(winners) == 1
        assert len(losers) == len(owners) - 1

        row = await _attempt_row(maker, attempt_id)
        assert row["lease_owner"] == winners[0].owner
        assert row["lease_generation"] == 1 == winners[0].generation
        assert row["lease_expires_at"] is not None

    async def test_stale_generation_cannot_transition_after_takeover(self, db_engine):
        engine, _ = db_engine
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        _, task_id = await _seed_task(maker)
        a = ExecutionLeaseStore(maker, owner="A")
        b = ExecutionLeaseStore(maker, owner="B")
        attempt_id = await a.create_attempt(task_id, str(uuid.uuid4()), 1)
        lease_a = await a.acquire(attempt_id, 600)

        with pytest.raises(LeaseNotAcquiredError):
            await b.acquire(attempt_id, 600)

        async with maker() as session:
            await session.execute(
                text(
                    "UPDATE task_execution_attempts SET lease_expires_at = now() - interval '1 second' "
                    "WHERE attempt_id = :a"
                ),
                {"a": attempt_id},
            )
            await session.commit()

        lease_b = await b.acquire(attempt_id, 600)
        assert lease_b.generation == lease_a.generation + 1

        with pytest.raises(LeaseLostError):
            await a.transition(lease_a, "pending", "running")
        await b.transition(lease_b, "pending", "running")
        with pytest.raises(LeaseLostError):
            await a.transition(lease_a, "running", "completed")
        await b.transition(lease_b, "running", "completed")

        row = await _attempt_row(maker, attempt_id)
        assert row["state"] == "completed"
        assert row["lease_owner"] == "B"
        assert row["lease_generation"] == 2

    async def test_expired_lease_rejects_transition_even_for_its_owner(self, db_engine):
        engine, _ = db_engine
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        _, task_id = await _seed_task(maker)
        store = ExecutionLeaseStore(maker, owner="A")
        lease = await store.begin(task_id, str(uuid.uuid4()), 1, 600)
        async with maker() as session:
            await session.execute(
                text(
                    "UPDATE task_execution_attempts SET lease_expires_at = now() - interval '1 second' "
                    "WHERE attempt_id = :a"
                ),
                {"a": lease.attempt_id},
            )
            await session.commit()
        with pytest.raises(LeaseLostError):
            await store.transition(lease, "running", "completed")
        assert (await _attempt_row(maker, lease.attempt_id))["state"] == "running"

    async def test_only_one_live_attempt_per_task(self, db_engine):
        engine, _ = db_engine
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        _, task_id = await _seed_task(maker)
        store = ExecutionLeaseStore(maker, owner="A")
        first = await store.begin(task_id, str(uuid.uuid4()), 1, 600)
        with pytest.raises(AttemptConflictError):
            await store.create_attempt(task_id, str(uuid.uuid4()), 1)
        await store.transition(first, "running", "failed")
        second = await store.begin(task_id, first.execution_id, 2, 600)
        assert second.attempt_id != first.attempt_id
        assert second.execution_id == first.execution_id


class _FakeLLM(BaseHTTPRequestHandler):
    reply = "Respuesta del modelo simulado."
    delay = 0.0
    requests: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "modelo-simulado", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(request)
        if self.delay:
            import time as _time

            _time.sleep(self.delay)
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            base = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "modelo-simulado"}
            chunks = [
                {
                    **base,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": self.reply}, "finish_reason": None}
                    ],
                },
                {
                    **base,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
            ]
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = json.dumps(
            {
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "modelo-simulado",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": self.reply}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _StubInstallation:
    def __init__(self, workspace: Path, base_url: str):
        self.workspace = workspace
        self.base_url = base_url

    def resolve_workspace_path(self, _path):
        return self.workspace

    def build_hermes_home(self, _path):
        home = self.workspace / ".hermes"
        home.mkdir(parents=True, exist_ok=True)
        return home

    async def build_process_env(self, _agent):
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.workspace),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        }

    def _model_provider_for_agent(self, _agent):
        return "custom"

    async def _effective_provider_base_url(self, _agent):
        return self.base_url


class TestRealRunnerIdentity:
    @pytest.fixture(autouse=True)
    def _requiere_hermes(self):
        pytest.importorskip("run_agent")

    async def test_real_runner_echoes_identity_and_attempt_is_persisted(self, db_engine, tmp_path):
        from hermeshq.services.hermes_runtime import HermesRuntime

        engine, _ = db_engine
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        agent_id, task_id = await _seed_task(maker, str(workspace))

        server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
        try:
            runtime = HermesRuntime(
                maker, secret_vault=None, installation_manager=_StubInstallation(workspace, base_url)
            )
            async with maker() as session:
                agent = await session.get(Agent, agent_id)
                task = await session.get(Task, task_id)
                agent.model = "modelo-simulado"
                agent.enabled_toolsets = []
                agent.max_iterations = 2

            deltas: list[str] = []

            async def on_delta(chunk, index=None):
                deltas.append(chunk)

            execution_id = str(uuid.uuid4())
            result = await runtime._run_real(
                agent,
                task,
                on_delta,
                "clave-simulada",
                "Eres un agente de prueba.",
                sys.executable,
                execution_id=execution_id,
                attempt_number=1,
            )
        finally:
            server.shutdown()

        assert "simulado" in result.final_response
        async with maker() as session:
            rows = (
                (
                    await session.execute(
                        text("SELECT * FROM task_execution_attempts WHERE task_id = :t"), {"t": task_id}
                    )
                )
                .mappings()
                .all()
            )
        assert len(rows) == 1
        row = rows[0]
        assert row["execution_id"] == execution_id
        assert result.execution_echo == {
            "execution_id": execution_id,
            "attempt_id": row["attempt_id"],
            "lease_generation": row["lease_generation"],
        }
        assert row["state"] == "completed"
        assert row["lease_generation"] == 1
        assert row["lease_owner"] == runtime.leases.owner
        assert row["runner_confirmed_at"] is not None
        assert row["finished_at"] is not None
        print("INTENTO_PERSISTIDO", json.dumps({k: str(v) for k, v in row.items()}, ensure_ascii=False))
