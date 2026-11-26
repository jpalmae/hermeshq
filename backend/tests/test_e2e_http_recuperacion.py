from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from conftest import _scratch_url, requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_execution_lease import _FakeLLM

from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.models.task import Task

pytestmark = [pytest.mark.integration, requires_database]

ADMIN_PASSWORD = "clave-admin-pruebas-e2e"
LAUNCHER = Path(__file__).resolve().parent / "e2e_http" / "instancia.py"
LEASE_ENV = {
    "EXECUTION_LEASE_TTL_SECONDS": "3",
    "EXECUTION_LEASE_RENEW_INTERVAL_SECONDS": "1",
    "RUNNER_HEARTBEAT_INTERVAL_SECONDS": "1",
    "RUNNER_HEARTBEAT_STALE_SECONDS": "2",
    "EXECUTION_RECONCILE_INTERVAL_SECONDS": "1",
    "EXECUTION_RECONCILE_GRACE_SECONDS": "1",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Instance:
    def __init__(
        self, name: str, db_url: str, workspace: Path, llm_url: str, log_dir: Path, extra_env: dict | None = None
    ):
        self.name = name
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = open(log_dir / f"{name}.log", "w")
        env = {
            **os.environ,
            **LEASE_ENV,
            "DATABASE_URL": db_url,
            "JWT_SECRET": "secreto-jwt-pruebas-e2e-" + "x" * 16,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
            "WORKSPACES_ROOT": str(workspace.parent),
            "HQ_E2E_WORKSPACE": str(workspace),
            "HQ_E2E_LLM_URL": llm_url,
            "TASK_PARALLEL_LANES": "true",
            "CONCURRENCY_INTERACTIVE_RESERVE": "2",
            **(extra_env or {}),
        }
        self.process = subprocess.Popen(
            [sys.executable, str(LAUNCHER), str(self.port)],
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.pgid = self.process.pid

    async def wait_ready(self, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError(f"{self.name} terminó al arrancar (ver {self.log.name})")
                try:
                    r = await client.get(f"{self.base}/api/auth/providers", timeout=2)
                    if r.status_code < 500:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise AssertionError(f"{self.name} no respondió")

    def crash_server_only(self) -> None:
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=10)

    def kill_group(self) -> None:
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if self.process.poll() is None:
            self.process.wait(timeout=10)
        self.log.close()


async def _login(base: str) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{base}/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _api(method: str, base: str, path: str, headers: dict, **kwargs) -> dict:
    async with httpx.AsyncClient() as client:
        r = await client.request(method, f"{base}/api{path}", headers=headers, timeout=15, **kwargs)
        r.raise_for_status()
        return r.json()


async def _attempts(maker, task_id: str):
    async with maker() as session:
        return (
            (
                await session.execute(
                    text("SELECT * FROM task_execution_attempts WHERE task_id = :t ORDER BY created_at"),
                    {"t": task_id},
                )
            )
            .mappings()
            .all()
        )


async def _until(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.3)
    raise AssertionError(f"tiempo agotado esperando: {what}")


def _requests_for(handler, marker: str) -> int:
    return sum(1 for r in handler.requests if marker in json.dumps(r))


@pytest.fixture
async def stack(db_engine, tmp_path):
    engine, db_name = db_engine
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    workspace = tmp_path / "workspaces" / "agente-e2e"
    workspace.mkdir(parents=True)
    handler = type("LLM", (_FakeLLM,), {"delay": 0.0, "requests": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    async with maker() as session:
        node = Node(name="nodo-e2e", hostname="localhost")
        session.add(node)
        await session.flush()
        agent = Agent(
            node_id=node.id,
            name="agente-e2e",
            slug="agente-e2e",
            workspace_path=str(workspace),
            status="running",
            model="modelo-simulado",
            enabled_toolsets=[],
            max_iterations=2,
        )
        session.add(agent)
        await session.commit()
        agent_id = agent.id
    instances: list[_Instance] = []

    def spawn(name: str) -> _Instance:
        inst = _Instance(name, _scratch_url(db_name), workspace, llm_url, tmp_path)
        instances.append(inst)
        return inst

    try:
        yield {"maker": maker, "handler": handler, "agent_id": agent_id, "spawn": spawn, "tmp": tmp_path}
    finally:
        for inst in instances:
            inst.kill_group()
        server.shutdown()


class TestHttpRecovery:
    async def test_task_survives_second_instance_start_and_completes_once(self, stack):
        maker, handler = stack["maker"], stack["handler"]
        handler.delay = 6.0
        a = stack["spawn"]("instancia-a")
        await a.wait_ready()
        headers = await _login(a.base)
        marker = f"marca-{uuid.uuid4().hex[:8]}"
        created = await _api(
            "POST", a.base, "/tasks", headers, json={"agent_id": stack["agent_id"], "prompt": f"Responde {marker}"}
        )
        task_id = created["id"]
        assert created["status"] == "queued"

        first = await _until(lambda: _attempts_with_heartbeat(maker, task_id, 1), 60, "primer latido")
        b = stack["spawn"]("instancia-b")
        await b.wait_ready()
        headers_b = await _login(b.base)
        mid = (await _attempts(maker, task_id))[-1]
        assert (mid["state"], mid["lease_generation"], mid["lease_owner"]) == (
            "running",
            first["lease_generation"],
            first["lease_owner"],
        )
        assert (await _api("GET", b.base, f"/tasks/{task_id}", headers_b))["status"] == "running"

        final = await _until(lambda: _final_status(b.base, headers_b, task_id), 60, "tarea completada")
        rows = await _attempts(maker, task_id)
        assert final["status"] == "completed"
        assert "simulado" in (final["response"] or "")
        assert len(rows) == 1
        assert (rows[0]["state"], rows[0]["lease_generation"]) == ("completed", 1)
        assert rows[0]["heartbeat_count"] >= 3
        assert _requests_for(handler, marker) == 1
        print(
            "HTTP_COMPLETADA",
            json.dumps(
                {
                    "latidos": rows[0]["heartbeat_count"],
                    "propietario": rows[0]["lease_owner"],
                    "estado_api": final["status"],
                }
            ),
        )

    async def test_crashed_instance_is_recovered_without_duplicate_or_stale_result(self, stack):
        maker, handler = stack["maker"], stack["handler"]
        handler.delay = 12.0
        a = stack["spawn"]("instancia-a")
        await a.wait_ready()
        headers = await _login(a.base)
        marker = f"marca-{uuid.uuid4().hex[:8]}"
        task_id = (
            await _api(
                "POST", a.base, "/tasks", headers, json={"agent_id": stack["agent_id"], "prompt": f"Responde {marker}"}
            )
        )["id"]
        running = await _until(lambda: _attempts_with_heartbeat(maker, task_id, 2), 60, "latidos")
        await _until(lambda: _async_true(_requests_for(handler, marker) == 1), 30, "llamada al modelo")

        a.crash_server_only()
        a_restarted = stack["spawn"]("instancia-a-reiniciada")
        await a_restarted.wait_ready()
        headers_r = await _login(a_restarted.base)

        final = await _until(lambda: _final_status(a_restarted.base, headers_r, task_id), 60, "recuperación")
        assert final["status"] == "completed"
        await asyncio.sleep(handler.delay + 3)
        after = await _api("GET", a_restarted.base, f"/tasks/{task_id}", headers_r)
        rows = await _attempts(maker, task_id)

        assert after["status"] == "completed"
        assert after["response"]
        assert len(rows) == 2
        assert rows[0]["attempt_id"] == running["attempt_id"]
        assert rows[0]["state"] == "abandoned"
        assert rows[0]["reconciliation"] == "interrumpida"
        assert rows[0]["lease_generation"] == running["lease_generation"] + 1
        assert rows[0]["runner_confirmed_at"] is None
        assert [r["state"] for r in rows[1:]] == ["completed"]
        assert _requests_for(handler, marker) == 2
        async with maker() as session:
            db_task = await session.get(Task, task_id)
        assert db_task.status == "completed"
        assert db_task.metadata_json["recovery_reintentos"] == 1
        print(
            "HTTP_RECUPERADA",
            json.dumps(
                {
                    "estado_api": after["status"],
                    "reintentos": db_task.metadata_json["recovery_reintentos"],
                    "intentos": [r["state"] for r in rows],
                },
                default=str,
            ),
        )


async def _async_true(value):
    return value


async def _attempts_with_heartbeat(maker, task_id: str, minimum: int):
    rows = await _attempts(maker, task_id)
    if rows and rows[-1]["state"] == "running" and rows[-1]["heartbeat_count"] >= minimum:
        return rows[-1]
    return None


async def _final_status(base: str, headers: dict, task_id: str):
    task = await _api("GET", base, f"/tasks/{task_id}", headers)
    return task if task["status"] in ("completed", "failed", "cancelled") else None


async def _schedule_tasks(maker, schedule_id: str) -> list[Task]:
    async with maker() as session:
        rows = (await session.execute(text("SELECT id FROM tasks ORDER BY queued_at"))).scalars().all()
        tasks = [await session.get(Task, r) for r in rows]
    return [t for t in tasks if (t.metadata_json or {}).get("scheduled_task_id") == schedule_id]


class TestCronSurvivesRestart:
    async def test_scheduled_run_interrupted_by_crash_is_retried_once_and_completes(self, stack):
        from hermeshq.models.scheduled_task import ScheduledTask

        maker, handler = stack["maker"], stack["handler"]
        handler.delay = 10.0
        a = stack["spawn"]("instancia-a")
        await a.wait_ready()
        headers = await _login(a.base)
        marker = f"cron-{uuid.uuid4().hex[:8]}"
        created = await _api(
            "POST",
            a.base,
            "/scheduled-tasks",
            headers,
            json={
                "agent_id": stack["agent_id"],
                "name": "informe programado",
                "cron_expression": "*/30 * * * *",
                "prompt": f"Prepara el informe {marker}",
                "timezone": "America/Santiago",
            },
        )
        schedule_id = created["id"]
        async with maker() as session:
            schedule = await session.get(ScheduledTask, schedule_id)
            assert schedule.approval_status == "approved"
            schedule.next_run = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        first = await _until(lambda: _first_scheduled(maker, schedule_id), 30, "disparo programado")
        await _until(lambda: _attempts_with_heartbeat(maker, first.id, 1), 60, "latido del intento programado")
        await _until(lambda: _async_true(_requests_for(handler, marker) == 1), 30, "llamada al modelo")

        a.crash_server_only()
        handler.delay = 0.5
        a_restarted = stack["spawn"]("instancia-a-reiniciada")
        b = stack["spawn"]("instancia-b")
        await asyncio.gather(a_restarted.wait_ready(), b.wait_ready())
        headers_r = await _login(a_restarted.base)

        async def _retry_done():
            tasks = await _schedule_tasks(maker, schedule_id)
            retries = [t for t in tasks if (t.metadata_json or {}).get("retry_of") == first.id]
            return retries[0] if retries and retries[0].status in ("completed", "failed") else None

        retry = await _until(_retry_done, 90, "reintento programado completado")
        await asyncio.sleep(8)

        tasks = await _schedule_tasks(maker, schedule_id)
        original = await _api("GET", a_restarted.base, f"/tasks/{first.id}", headers_r)
        retry_api = await _api("GET", a_restarted.base, f"/tasks/{retry.id}", headers_r)
        async with maker() as session:
            schedule = await session.get(ScheduledTask, schedule_id)
        assert [t.id for t in tasks] == [first.id, retry.id]
        assert original["status"] == "failed"
        assert retry_api["status"] == "completed"
        assert "simulado" in (retry_api["response"] or "")
        assert retry.metadata_json["scheduled_fire_key"] == first.metadata_json["scheduled_fire_key"] + "#reintento"
        first_attempts = await _attempts(maker, first.id)
        retry_attempts = await _attempts(maker, retry.id)
        assert [(r["state"], r["reconciliation"]) for r in first_attempts] == [("abandoned", "interrumpida")]
        assert [r["state"] for r in retry_attempts] == ["completed"]
        assert _requests_for(handler, marker) == 2
        assert schedule.next_run > datetime.now(UTC)
        print(
            "CRON_REINICIO",
            json.dumps(
                {
                    "disparo": first.metadata_json["scheduled_fire_key"],
                    "original": original["status"],
                    "reintento": retry_api["status"],
                    "llamadas_modelo": _requests_for(handler, marker),
                    "proximo": schedule.next_run.isoformat(),
                }
            ),
        )


async def _first_scheduled(maker, schedule_id: str):
    tasks = await _schedule_tasks(maker, schedule_id)
    return tasks[0] if tasks else None
