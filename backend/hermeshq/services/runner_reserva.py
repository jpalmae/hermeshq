from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

VARIABLES_DE_INTENTO = frozenset({"HERMESHQ_EXECUTION_ID", "HERMESHQ_ATTEMPT_ID", "HERMESHQ_LEASE_GENERATION"})
RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "hermes_task_runner.py"
CWD = Path(__file__).resolve().parents[2]


@dataclass
class RunnerEnReserva:
    proceso: asyncio.subprocess.Process
    read_fd: int
    write_fd: int
    creado: float

    def vivo(self) -> bool:
        return self.proceso.returncode is None

    def descartar(self) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            self.proceso.kill()
        for fd in (self.read_fd, self.write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def entorno_base(process_env: dict) -> dict:
    return {k: v for k, v in process_env.items() if k not in VARIABLES_DE_INTENTO}


def clave(agent_id: str, python_bin: str, entorno: dict) -> str:
    huella = hashlib.sha256(json.dumps(sorted(entorno.items()), default=str).encode()).hexdigest()
    return f"{agent_id}|{python_bin}|{huella}"


class ReservaDeRunners:
    def __init__(self, ttl_segundos: int = 300, maximo: int = 4) -> None:
        self.ttl = max(1, int(ttl_segundos))
        self.maximo = max(0, int(maximo))
        self._reservas: dict[str, RunnerEnReserva] = {}
        self._preparando: set[str] = set()

    def tomar(self, clave_reserva: str) -> RunnerEnReserva | None:
        entrada = self._reservas.pop(clave_reserva, None)
        if entrada is None:
            return None
        if not entrada.vivo() or time.monotonic() - entrada.creado > self.ttl:
            entrada.descartar()
            return None
        return entrada

    def purgar(self) -> None:
        ahora = time.monotonic()
        for k, entrada in list(self._reservas.items()):
            if not entrada.vivo() or ahora - entrada.creado > self.ttl:
                self._reservas.pop(k, None)
                entrada.descartar()

    async def preparar(self, clave_reserva: str, python_bin: str, entorno: dict) -> None:
        self.purgar()
        if clave_reserva in self._reservas or clave_reserva in self._preparando:
            return
        if len(self._reservas) + len(self._preparando) >= self.maximo:
            return
        self._preparando.add(clave_reserva)
        read_fd = write_fd = -1
        try:
            read_fd, write_fd = os.pipe()
            os.set_inheritable(read_fd, False)
            proceso = await asyncio.create_subprocess_exec(
                python_bin,
                str(RUNNER),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
                cwd=str(CWD),
                env={**entorno, "HERMESHQ_RUNNER_RESERVA": "1"},
                pass_fds=(write_fd,),
            )
            self._reservas[clave_reserva] = RunnerEnReserva(proceso, read_fd, write_fd, time.monotonic())
        except Exception as exc:  # noqa: BLE001
            logger.warning("No se pudo preparar un runner en reserva: %s", exc)
            for fd in (read_fd, write_fd):
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
        finally:
            self._preparando.discard(clave_reserva)

    def cerrar(self) -> None:
        for k in list(self._reservas):
            self._reservas.pop(k).descartar()


_RESERVA: ReservaDeRunners | None = None


def reserva_activa() -> ReservaDeRunners | None:
    from hermeshq.config import get_settings

    ajustes = get_settings()
    if not ajustes.runner_reserva_enabled:
        return None
    global _RESERVA
    if _RESERVA is None:
        _RESERVA = ReservaDeRunners(ajustes.runner_reserva_ttl_seconds, ajustes.runner_reserva_max)
    return _RESERVA
