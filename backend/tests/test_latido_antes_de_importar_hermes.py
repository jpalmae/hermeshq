from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from hermeshq.services.runner_heartbeat import HeartbeatChannel

RUNNER = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "hermes_task_runner.py"

IMPORT_BLOQUEADO = textwrap.dedent(
    """
    import os, time
    open(os.environ["HQ_PRUEBA_IMPORTANDO"], "w").close()
    limite = time.time() + 60
    while not os.path.exists(os.environ["HQ_PRUEBA_LIBERAR"]) and time.time() < limite:
        time.sleep(0.05)
    _HERMES_PROVIDER_ENV_BLOCKLIST = frozenset()
    """
)


def _latido(read_fd: int, canal: HeartbeatChannel, espera: float) -> int | None:
    limite = time.monotonic() + espera
    pendiente = b""
    while time.monotonic() < limite:
        listos, _, _ = select.select([read_fd], [], [], 0.2)
        if not listos:
            continue
        datos = os.read(read_fd, 4096)
        if not datos:
            return None
        pendiente += datos
        while b"\n" in pendiente:
            linea, pendiente = pendiente.split(b"\n", 1)
            seq = canal.verify(linea)
            if seq is not None:
                canal.last_seq = seq
                return seq
    return None


def test_el_runner_late_mientras_importa_hermes(tmp_path):
    paquete = tmp_path / "falso" / "tools" / "environments"
    paquete.mkdir(parents=True)
    (tmp_path / "falso" / "tools" / "__init__.py").write_text("")
    (paquete / "__init__.py").write_text("")
    (paquete / "local.py").write_text(IMPORT_BLOQUEADO)
    importando, liberar = tmp_path / "importando", tmp_path / "liberar"

    canal = HeartbeatChannel(execution_id="e1", attempt_id="a1", generation=1, max_age_seconds=30)
    write_fd = canal.open()
    read_fd = canal._read_fd
    payload = {
        "hermes_home": str(tmp_path / ".hermes"),
        "cwd": str(tmp_path),
        "prompt": "hola",
        "model": "m",
        "execution": {
            "execution_id": "e1",
            "attempt_id": "a1",
            "lease_generation": 1,
            **canal.runner_config(1),
        },
    }
    entorno = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(tmp_path / "falso"), os.environ.get("PYTHONPATH", "")]),
        "HQ_PRUEBA_IMPORTANDO": str(importando),
        "HQ_PRUEBA_LIBERAR": str(liberar),
    }
    proceso = subprocess.Popen(
        [sys.executable, str(RUNNER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=entorno,
        pass_fds=(write_fd,),
        cwd=str(tmp_path),
    )
    try:
        os.close(write_fd)
        proceso.stdin.write(json.dumps(payload).encode())
        proceso.stdin.close()
        assert _latido(read_fd, canal, 30) == 1, "el runner no latió antes de terminar de importar Hermes"
        limite = time.monotonic() + 30
        while not importando.exists() and time.monotonic() < limite:
            time.sleep(0.05)
        assert importando.exists(), "el runner no llegó a importar Hermes"
        seq = _latido(read_fd, canal, 10)
        assert seq is not None and seq > 1, "el latido se detuvo durante el import"
        assert not liberar.exists()
    finally:
        liberar.touch()
        proceso.kill()
        proceso.wait(timeout=10)
        os.close(read_fd)
