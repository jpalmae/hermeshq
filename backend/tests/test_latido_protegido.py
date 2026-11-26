from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest
from conftest import requires_database
from test_execution_lease import _seed_task
from test_execution_lease_e2e import (
    CFG,
    _attempts,
    _Env,
    _maker,
    _run,
    _wait_heartbeat,
    _wait_runner,
)

from hermeshq.services import hermes_runtime as runtime_module
from hermeshq.services.hermes_runtime import RuntimeExecutionError
from hermeshq.services.runner_heartbeat import HeartbeatChannel, heartbeat_mac

FALSIFICADOR_ARCHIVO = textwrap.dedent(
    """
    import json, os, sys, time
    home, attempt_id, execution_id, generation, salida = sys.argv[1:6]
    os.makedirs(os.path.join(home, "latidos"), exist_ok=True)
    ruta = os.path.join(home, "latidos", attempt_id + ".json")
    escritos = 0
    fin = time.time() + float(sys.argv[6])
    seq = 1000
    while time.time() < fin:
        seq += 1
        datos = {"execution_id": execution_id, "attempt_id": attempt_id, "lease_generation": int(generation),
                 "seq": seq, "ts": f"{time.time():.3f}", "mac": "0" * 64}
        with open(ruta, "w") as f:
            f.write(json.dumps(datos))
        with open(os.path.join(home, "..", "notas.txt"), "a") as f:
            f.write("x")
        escritos += 1
        time.sleep(0.2)
    open(salida, "w").write(json.dumps({"escritos": escritos, "ruta": ruta}))
    """
)

FALSIFICADOR_PROC = textwrap.dedent(
    """
    import json, os, sys, time
    pid, attempt_id, execution_id, generation, salida, duracion = sys.argv[1:7]
    tuberias = {}
    try:
        nombres = os.listdir(f"/proc/{pid}/fd")
    except OSError as exc:
        nombres = []
        tuberias["listado"] = repr(exc)
    for nombre in nombres:
        ruta = f"/proc/{pid}/fd/{nombre}"
        try:
            destino = os.readlink(ruta)
        except OSError:
            continue
        if not destino.startswith("pipe:"):
            continue
        try:
            tuberias[nombre] = os.open(ruta, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            tuberias[nombre] = repr(exc)
    abiertas = {k: v for k, v in tuberias.items() if isinstance(v, int)}
    escritos = 0
    seq = 1000
    fin = time.time() + float(duracion)
    while time.time() < fin:
        seq += 1
        linea = json.dumps({"execution_id": execution_id, "attempt_id": attempt_id,
                            "lease_generation": int(generation), "seq": seq, "ts": f"{time.time():.3f}",
                            "mac": "f" * 64}) + "\\n"
        for fd in abiertas.values():
            try:
                os.write(fd, linea.encode())
                escritos += 1
            except OSError:
                pass
        time.sleep(0.2)
    open(salida, "w").write(json.dumps({"tuberias": {k: (v if isinstance(v, str) else "abierta")
                                                     for k, v in tuberias.items()}, "escritos": escritos}))
    """
)


class TestHeartbeatVerification:
    def _channel(self) -> HeartbeatChannel:
        return HeartbeatChannel("exec-1", "att-1", 3, max_age_seconds=5)

    def _line(self, ch: HeartbeatChannel, seq: int, ts: float | None = None, key: str | None = None, **over) -> bytes:
        ts_s = f"{(ts if ts is not None else time.time()):.3f}"
        data = {"execution_id": "exec-1", "attempt_id": "att-1", "lease_generation": 3, "seq": seq, "ts": ts_s}
        data.update(over)
        data["mac"] = heartbeat_mac(
            key or ch.key, data["execution_id"], data["attempt_id"], data["lease_generation"], seq, ts_s
        )
        return json.dumps(data).encode()

    def test_valid_signed_heartbeat_is_accepted(self):
        ch = self._channel()
        ch.feed(self._line(ch, 1))
        ch.feed(self._line(ch, 2))
        assert (ch.last_seq, ch.accepted, ch.rejected) == (2, 2, 0)

    def test_forged_unsigned_or_wrong_key_is_rejected(self):
        ch = self._channel()
        ch.feed(self._line(ch, 5, key="otra-clave"))
        forged = json.loads(self._line(ch, 6))
        forged["mac"] = "0" * 64
        ch.feed(json.dumps(forged).encode())
        unsigned = {k: v for k, v in forged.items() if k != "mac"}
        ch.feed(json.dumps(unsigned).encode())
        assert (ch.last_seq, ch.accepted, ch.rejected) == (0, 0, 3)

    def test_identity_mismatch_stale_and_replay_are_rejected(self):
        ch = self._channel()
        ch.feed(self._line(ch, 1, attempt_id="att-otro"))
        ch.feed(self._line(ch, 1, lease_generation=2))
        ch.feed(self._line(ch, 1, ts=time.time() - 60))
        good = self._line(ch, 2)
        ch.feed(good)
        ch.feed(good)
        ch.feed(self._line(ch, 1))
        assert (ch.last_seq, ch.accepted, ch.rejected) == (2, 1, 5)


@pytest.fixture
def fast_leases(monkeypatch):
    monkeypatch.setattr("hermeshq.services.hermes_runtime.lease_settings", lambda: dict(CFG))
    monkeypatch.setattr("hermeshq.services.execution_lease.lease_settings", lambda: dict(CFG))
    return CFG


@pytest.fixture
def channels(monkeypatch):
    created: list[HeartbeatChannel] = []

    class Recording(HeartbeatChannel):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.leaked_fd = -1
            created.append(self)

        def open(self) -> int:
            fd = super().open()
            self.leaked_fd = os.dup(fd)
            return fd

    monkeypatch.setattr(runtime_module, "HeartbeatChannel", Recording)
    return created


def _fdinfo_flags(pid: int, fd: int) -> int:
    with open(f"/proc/{pid}/fdinfo/{fd}") as f:
        for line in f:
            if line.startswith("flags:"):
                return int(line.split()[1], 8)
    raise AssertionError("sin flags")


@pytest.mark.integration
@requires_database
class TestHeartbeatCannotBeForged:
    @pytest.fixture(autouse=True)
    def _requiere_hermes(self):
        pytest.importorskip("run_agent")

    async def test_workspace_file_forgery_does_not_keep_a_stopped_runner_alive(
        self, db_engine, tmp_path, fast_leases, channels
    ):
        maker = _maker(db_engine)
        env = _Env(maker, tmp_path, delay=30.0)
        report = tmp_path / "falsificador.json"
        try:
            agent_id, task_id = await _seed_task(maker, str(env.workspace))
            agent, task = await env.load(agent_id, task_id)
            runtime = env.runtime()
            job = asyncio.create_task(_run(runtime, agent, task))
            entry = await _wait_runner(runtime)
            row = await _wait_heartbeat(maker, task_id, 1)
            home = env.workspace / ".hermes"
            assert not (home / "latidos").exists()
            entry.process.send_signal(signal.SIGSTOP)
            stopped_at = row["heartbeat_count"]
            forger = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    FALSIFICADOR_ARCHIVO,
                    str(home),
                    row["attempt_id"],
                    row["execution_id"],
                    str(row["lease_generation"]),
                    str(report),
                    "6",
                ],
            )
            with pytest.raises(RuntimeExecutionError) as info:
                await asyncio.wait_for(job, 30)
            forger.wait(timeout=15)
        finally:
            env.close()
        forged = json.loads(report.read_text())
        assert forged["escritos"] >= 5
        assert getattr(info.value, "hq_end_reason_hint", None) == "runner_sin_latido"
        rows = await _attempts(maker, task_id)
        assert [r["state"] for r in rows] == ["failed"]
        assert rows[0]["heartbeat_count"] <= stopped_at + 1
        assert channels[0].rejected == 0
        print(
            "FALSIFICACION_ARCHIVO",
            json.dumps({**forged, "renovaciones_tras_detener": rows[0]["heartbeat_count"] - stopped_at}),
        )

    async def test_attacker_holding_the_pipe_or_proc_cannot_forge_heartbeats(
        self, db_engine, tmp_path, fast_leases, channels
    ):
        maker = _maker(db_engine)
        env = _Env(maker, tmp_path, delay=30.0)
        report = tmp_path / "falsificador-proc.json"
        try:
            agent_id, task_id = await _seed_task(maker, str(env.workspace))
            agent, task = await env.load(agent_id, task_id)
            runtime = env.runtime()
            job = asyncio.create_task(_run(runtime, agent, task))
            entry = await _wait_runner(runtime)
            row = await _wait_heartbeat(maker, task_id, 1)
            pid = entry.process.pid
            try:
                _fdinfo_flags(pid, channels[0].runner_fd)
                proc_legible = True
            except PermissionError:
                proc_legible = False
            entry.process.send_signal(signal.SIGSTOP)
            stopped_at = row["heartbeat_count"]
            forger = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    FALSIFICADOR_PROC,
                    str(pid),
                    row["attempt_id"],
                    row["execution_id"],
                    str(row["lease_generation"]),
                    str(report),
                    "6",
                ],
            )
            leaked = channels[0].leaked_fd
            seq = 5000
            while not job.done():
                seq += 1
                ts = f"{time.time():.3f}"
                forged = {
                    "execution_id": row["execution_id"],
                    "attempt_id": row["attempt_id"],
                    "lease_generation": row["lease_generation"],
                    "seq": seq,
                    "ts": ts,
                    "mac": heartbeat_mac(
                        "clave-adivinada", row["execution_id"], row["attempt_id"], row["lease_generation"], seq, ts
                    ),
                }
                try:
                    os.write(leaked, (json.dumps(forged) + "\n").encode())
                except OSError:
                    break
                await asyncio.sleep(0.2)
            with pytest.raises(RuntimeExecutionError) as info:
                await asyncio.wait_for(job, 30)
            forger.wait(timeout=15)
        finally:
            env.close()
            if channels and channels[0].leaked_fd >= 0:
                os.close(channels[0].leaked_fd)
        forged_proc = json.loads(report.read_text())
        assert proc_legible is False, "el runner debe ser no volcable: /proc/<pid>/fd* no accesible"
        assert forged_proc["escritos"] == 0
        assert forged_proc["tuberias"] and all("PermissionError" in v for v in forged_proc["tuberias"].values())
        assert getattr(info.value, "hq_end_reason_hint", None) == "runner_sin_latido"
        rows = await _attempts(maker, task_id)
        assert [r["state"] for r in rows] == ["failed"]
        assert rows[0]["heartbeat_count"] <= stopped_at + 1
        assert channels[0].rejected >= 5
        assert channels[0].last_seq < 5000
        print(
            "FALSIFICACION_CANAL",
            json.dumps(
                {
                    "proc": forged_proc,
                    "rechazados_por_firma": channels[0].rejected,
                    "ultimo_seq_valido": channels[0].last_seq,
                }
            ),
        )


def test_runner_heartbeat_key_never_reaches_env_or_files(tmp_path):
    from hermeshq.scripts import hermes_task_runner as runner

    read_fd, write_fd = os.pipe()
    runner._IDENTIDAD_EJECUCION = {"execution_id": "e", "attempt_id": "a", "lease_generation": 1}
    try:
        identidad = {"heartbeat_fd": write_fd, "heartbeat_key": "k" * 64, "heartbeat_interval": 1}
        runner._iniciar_latido(identidad.get("heartbeat_fd"), identidad.pop("heartbeat_key", None), 0.2)
        assert "heartbeat_key" not in identidad
        assert os.get_inheritable(write_fd) is False
        line = os.read(read_fd, 4096).split(b"\n")[0]
        ch = HeartbeatChannel("e", "a", 1, max_age_seconds=5, key="k" * 64)
        assert ch.verify(line) == 1
        assert "k" * 64 not in json.dumps(dict(os.environ))
    finally:
        runner._IDENTIDAD_EJECUCION = None
        os.close(read_fd)
