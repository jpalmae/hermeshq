from __future__ import annotations

import asyncio
import json
import signal
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import requires_database
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_execution_lease import _FakeLLM, _seed_task, _StubInstallation

from hermeshq.models.agent import Agent
from hermeshq.models.task import Task
from hermeshq.services.execution_lease import (
    AttemptConflictError,
    ExecutionLeaseStore,
    IdentityUnconfirmedError,
    LeaseLostError,
)
from hermeshq.services.hermes_runtime import HermesRuntime, RuntimeExecutionError

pytestmark = [pytest.mark.integration, requires_database]

CFG = {
    "ttl": 3,
    "renew_interval": 1,
    "heartbeat_interval": 1,
    "heartbeat_stale": 2,
    "reconcile_interval": 1,
    "reconcile_grace": 0,
}


@pytest.fixture
def fast_leases(monkeypatch):
    monkeypatch.setattr("hermeshq.services.hermes_runtime.lease_settings", lambda: dict(CFG))
    monkeypatch.setattr("hermeshq.services.execution_lease.lease_settings", lambda: dict(CFG))
    return CFG


class _Env:
    def __init__(self, maker, tmp_path: Path, delay: float = 0.0):
        self.maker = maker
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir()
        handler = type("LLM", (_FakeLLM,), {"delay": delay, "requests": []})
        self.handler = handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def runtime(self) -> HermesRuntime:
        return HermesRuntime(
            self.maker, secret_vault=None, installation_manager=_StubInstallation(self.workspace, self.base_url)
        )

    async def load(self, agent_id: str, task_id: str):
        async with self.maker() as session:
            agent = await session.get(Agent, agent_id)
            task = await session.get(Task, task_id)
            agent.model = "modelo-simulado"
            agent.enabled_toolsets = []
            agent.max_iterations = 2
            return agent, task

    def close(self):
        self.server.shutdown()


async def _run(runtime: HermesRuntime, agent, task, python_bin: str = sys.executable, deltas=None):
    async def on_delta(chunk, index=None):
        if deltas is not None:
            deltas.append((time.monotonic(), chunk))

    return await runtime._run_real(
        agent,
        task,
        on_delta,
        "clave-simulada",
        "Agente de prueba.",
        python_bin,
        execution_id=str(uuid.uuid4()),
        attempt_number=1,
    )


async def _attempts(maker, task_id: str):
    async with maker() as session:
        return (
            (
                await session.execute(
                    text("SELECT * FROM task_execution_attempts WHERE task_id = :t ORDER BY created_at"), {"t": task_id}
                )
            )
            .mappings()
            .all()
        )


async def _task(maker, task_id: str) -> Task:
    async with maker() as session:
        return await session.get(Task, task_id)


async def _wait_runner(runtime: HermesRuntime, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in runtime.local_runners.runners.values():
            return entry
        await asyncio.sleep(0.1)
    raise AssertionError("el runner no arrancó")


async def _wait_heartbeat(maker, task_id: str, minimum: int = 1, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = await _attempts(maker, task_id)
        if rows and rows[-1]["heartbeat_count"] >= minimum:
            return rows[-1]
        await asyncio.sleep(0.2)
    raise AssertionError("no hubo latidos")


async def _wait_llm(env, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if env.handler.requests:
            return
        await asyncio.sleep(0.1)
    raise AssertionError("el runner no llegó a llamar al modelo")


def _maker(db_engine):
    engine, _ = db_engine
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class TestHeartbeatRealRunner:
    async def test_silent_long_task_keeps_its_lease_by_renewal(self, db_engine, tmp_path, fast_leases):
        maker = _maker(db_engine)
        env = _Env(maker, tmp_path, delay=9.0)
        try:
            agent_id, task_id = await _seed_task(maker, str(env.workspace))
            agent, task = await env.load(agent_id, task_id)
            deltas: list = []
            started = time.monotonic()
            result = await _run(env.runtime(), agent, task, deltas=deltas)
            elapsed = time.monotonic() - started
        finally:
            env.close()
        rows = await _attempts(maker, task_id)
        assert len(rows) == 1
        row = rows[0]
        assert "simulado" in result.final_response
        assert elapsed > 3 * CFG["ttl"] - 1
        assert all(t - started > 8 for t, _ in deltas)
        assert row["state"] == "completed"
        assert row["lease_generation"] == 1
        assert row["heartbeat_count"] >= 5
        assert row["runner_heartbeat_seq"] >= 5
        assert (row["last_heartbeat_at"] - row["created_at"]).total_seconds() > CFG["ttl"]
        assert row["runner_confirmed_at"] is not None
        assert len(env.handler.requests) == 1
        print(
            "SILENCIOSA",
            json.dumps(
                {
                    "segundos": round(elapsed, 1),
                    "renovaciones": row["heartbeat_count"],
                    "latidos_runner": row["runner_heartbeat_seq"],
                    "ttl": CFG["ttl"],
                    "estado": row["state"],
                }
            ),
        )

    async def test_stopped_heartbeat_stops_the_runner_and_fails_the_attempt(self, db_engine, tmp_path, fast_leases):
        maker = _maker(db_engine)
        env = _Env(maker, tmp_path, delay=30.0)
        try:
            agent_id, task_id = await _seed_task(maker, str(env.workspace))
            agent, task = await env.load(agent_id, task_id)
            runtime = env.runtime()
            job = asyncio.create_task(_run(runtime, agent, task))
            entry = await _wait_runner(runtime)
            await _wait_heartbeat(maker, task_id, 1)
            entry.process.send_signal(signal.SIGSTOP)
            with pytest.raises(RuntimeExecutionError) as info:
                await asyncio.wait_for(job, 30)
        finally:
            env.close()
        assert getattr(info.value, "hq_end_reason_hint", None) == "runner_sin_latido"
        assert entry.process.returncode is not None
        rows = await _attempts(maker, task_id)
        assert [r["state"] for r in rows] == ["failed"]
        assert rows[0]["end_reason"] == "runner_sin_latido"

    async def test_expired_lease_mid_run_fences_the_runner_and_drops_the_result(self, db_engine, tmp_path, fast_leases):
        maker = _maker(db_engine)
        env = _Env(maker, tmp_path, delay=6.0)
        try:
            agent_id, task_id = await _seed_task(maker, str(env.workspace))
            agent, task = await env.load(agent_id, task_id)
            runtime = env.runtime()
            job = asyncio.create_task(_run(runtime, agent, task))
            entry = await _wait_runner(runtime)
            await _wait_heartbeat(maker, task_id, 1)
            async with maker() as session:
                await session.execute(
                    text(
                        "UPDATE task_execution_attempts SET lease_expires_at = now() - interval '1 second' "
                        "WHERE task_id = :t"
                    ),
                    {"t": task_id},
                )
                await session.commit()
            with pytest.raises(LeaseLostError):
                await asyncio.wait_for(job, 30)
        finally:
            env.close()
        assert entry.process.returncode is not None
        assert entry.fenced_reason == "lease_perdido"
        rows = await _attempts(maker, task_id)
        assert [r["state"] for r in rows] == ["lost"]
        assert rows[0]["runner_confirmed_at"] is None

    async def test_two_workers_start_the_same_task_only_one_runner_runs(self, db_engine, tmp_path, fast_leases):
        maker = _maker(db_engine)
        env = _Env(maker, tmp_path, delay=2.0)
        try:
            agent_id, task_id = await _seed_task(maker, str(env.workspace))
            agent, task = await env.load(agent_id, task_id)
            a, b = env.runtime(), env.runtime()
            b.leases.owner = "worker-b"
            results = await asyncio.gather(_run(a, agent, task), _run(b, agent, task), return_exceptions=True)
        finally:
            env.close()
        ok = [r for r in results if not isinstance(r, Exception)]
        conflicts = [r for r in results if isinstance(r, AttemptConflictError)]
        assert len(ok) == 1 and len(conflicts) == 1
        rows = await _attempts(maker, task_id)
        assert [r["state"] for r in rows] == ["completed"]
        assert len(env.handler.requests) == 1


_FAKE_RUNNER = r"""#!{python}
import json, sys
payload = json.loads(sys.stdin.read())
ident = {{k: payload["execution"][k] for k in ("execution_id", "attempt_id", "lease_generation")}}
mode = "{mode}"
if mode == "started_then_stream":
    print(json.dumps({{"event": "attempt_started", "execution": ident}}), flush=True)
print(json.dumps({{"event": "delta", "data": "Listo, el informe quedó guardado en tu carpeta."}}), flush=True)
if mode == "result_without_echo":
    print(json.dumps({{"event": "result", "final_response": "hecho", "messages": [], "tool_calls": []}}), flush=True)
if mode == "result_forged":
    print(json.dumps({{"event": "result", "final_response": "hecho", "messages": [], "tool_calls": [],
                      "execution": {{**ident, "lease_generation": ident["lease_generation"] + 1}}}}), flush=True)
sys.exit(0)
"""


def _fake_runner(tmp_path: Path, mode: str) -> str:
    path = tmp_path / f"runner_{mode}.py"
    path.write_text(_FAKE_RUNNER.format(python=sys.executable, mode=mode))
    path.chmod(0o755)
    return str(path)


class TestIdentityContract:
    @pytest.mark.parametrize("mode", ["result_without_echo", "stream_without_started", "result_forged"])
    async def test_results_without_valid_identity_are_rejected(self, db_engine, tmp_path, fast_leases, mode):
        maker = _maker(db_engine)
        env = _Env(maker, tmp_path)
        try:
            agent_id, task_id = await _seed_task(maker, str(env.workspace))
            agent, task = await env.load(agent_id, task_id)
            with pytest.raises(IdentityUnconfirmedError):
                await _run(env.runtime(), agent, task, python_bin=_fake_runner(tmp_path, mode))
        finally:
            env.close()
        rows = await _attempts(maker, task_id)
        assert [(r["state"], r["end_reason"]) for r in rows] == [("failed", "identidad_no_confirmada")]
        assert rows[0]["runner_confirmed_at"] is None

    async def test_stream_recovery_is_accepted_only_with_runner_identity(self, db_engine, tmp_path, fast_leases):
        maker = _maker(db_engine)
        env = _Env(maker, tmp_path)
        try:
            agent_id, task_id = await _seed_task(maker, str(env.workspace))
            agent, task = await env.load(agent_id, task_id)
            result = await _run(env.runtime(), agent, task, python_bin=_fake_runner(tmp_path, "started_then_stream"))
        finally:
            env.close()
        assert "informe" in result.final_response
        rows = await _attempts(maker, task_id)
        assert rows[0]["state"] == "completed" and rows[0]["runner_confirmed_at"] is not None


async def _orphan(maker, task_id: str, owner: str, tool_events: int = 0, ttl: int = 2):
    store = ExecutionLeaseStore(maker, owner=owner)
    lease = await store.begin(task_id, str(uuid.uuid4()), 1, ttl)
    if tool_events:
        await store.renew(lease, ttl, runner_seq=1, tool_events=tool_events)
    return store, lease


class TestLeaseSettings:
    def test_stale_heartbeat_window_must_fit_inside_the_lease(self):
        from hermeshq.services.execution_lease import validate_lease_settings

        assert validate_lease_settings(dict(CFG)) == CFG
        with pytest.raises(ValueError):
            validate_lease_settings({**CFG, "heartbeat_stale": 4})
        with pytest.raises(ValueError):
            validate_lease_settings({**CFG, "heartbeat_stale": 1})
        with pytest.raises(ValueError):
            validate_lease_settings({**CFG, "renew_interval": 0})

    def test_default_settings_are_valid(self):
        from hermeshq.services.execution_lease import lease_settings

        cfg = lease_settings()
        assert cfg["ttl"] > cfg["heartbeat_stale"] > cfg["heartbeat_interval"]

    async def test_lease_lost_after_expiry_gets_an_explicit_state(self, db_engine):
        maker = _maker(db_engine)
        _, task_id = await _seed_task(maker)
        store, lease = await _orphan(maker, task_id, "propio", ttl=1)
        await asyncio.sleep(1.2)
        assert await store.close_quietly(lease, "failed", end_reason="runner_sin_latido") is False
        rows = await _attempts(maker, task_id)
        assert (rows[0]["state"], rows[0]["end_reason"]) == ("lost", "runner_sin_latido")
