from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CHAT = "chat"
CRON = "cron"
SEGUNDO_PLANO = "segundo_plano"
CLASES = (CHAT, CRON, SEGUNDO_PLANO)
ENVEJECIMIENTO_POR_DEFECTO_S = 300.0


@dataclass(frozen=True)
class Limites:
    total: int
    reserva: int
    no_interactivo: int
    segundo_plano: int
    envejecimiento_s: float
    chat_preferente: bool


def _entero(valor, por_defecto: int) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return por_defecto


def normalizar(total, reserva, tope, envejecimiento=ENVEJECIMIENTO_POR_DEFECTO_S) -> tuple[Limites, list[str]]:
    avisos: list[str] = []
    total_ef = _entero(total, 1)
    if total_ef < 1:
        avisos.append(f"CONCURRENCY_SEMAPHORE={total!r} no es válido; se usa 1")
        total_ef = 1
    reserva_ef = _entero(reserva, 0)
    if reserva_ef < 0:
        avisos.append(f"CONCURRENCY_INTERACTIVE_RESERVE={reserva!r} es negativa; se usa 0")
        reserva_ef = 0
    chat_preferente = False
    if reserva_ef > total_ef - 1:
        avisos.append(
            f"CONCURRENCY_INTERACTIVE_RESERVE={reserva_ef} no deja cupo a crons ni segundo plano con "
            f"CONCURRENCY_SEMAPHORE={total_ef}; se usa {total_ef - 1}"
            + ("; el chat pasa primero en la cola del único cupo" if total_ef == 1 else "")
        )
        chat_preferente = total_ef == 1
        reserva_ef = total_ef - 1
    no_interactivo = total_ef - reserva_ef
    tope_ef = _entero(tope, 0)
    if tope_ef < 0:
        avisos.append(f"CONCURRENCY_BACKGROUND_MAX={tope!r} es negativo; se usa el valor por defecto")
        tope_ef = 0
    if no_interactivo >= 2:
        maximo = no_interactivo - 1
        segundo_plano = maximo if tope_ef == 0 else min(tope_ef, maximo)
        if tope_ef > maximo:
            avisos.append(
                f"CONCURRENCY_BACKGROUND_MAX={tope_ef} dejaría a los crons sin cupo propio (cupo no interactivo "
                f"{no_interactivo}); se usa {maximo}"
            )
    else:
        segundo_plano = 1
        avisos.append(
            "Cupo no interactivo de 1: crons y segundo plano comparten un solo cupo y un cron puede esperar a que "
            "termine el trabajo en segundo plano en curso; sube CONCURRENCY_SEMAPHORE o baja "
            "CONCURRENCY_INTERACTIVE_RESERVE para darles un cupo propio"
        )
    try:
        envejecimiento_ef = float(envejecimiento)
    except (TypeError, ValueError):
        envejecimiento_ef = ENVEJECIMIENTO_POR_DEFECTO_S
    if envejecimiento_ef < 0:
        avisos.append(f"CONCURRENCY_AGING_SECONDS={envejecimiento!r} es negativo; se usa 0")
        envejecimiento_ef = 0.0
    limites = Limites(
        total=total_ef,
        reserva=reserva_ef,
        no_interactivo=no_interactivo,
        segundo_plano=segundo_plano,
        envejecimiento_s=envejecimiento_ef,
        chat_preferente=chat_preferente,
    )
    return limites, avisos


class _Espera:
    __slots__ = ("clase", "llegada", "futuro")

    def __init__(self, clase: str, llegada: float, futuro: asyncio.Future) -> None:
        self.clase = clase
        self.llegada = llegada
        self.futuro = futuro


class ControlAdmision:
    def __init__(self, limites: Limites, reloj=time.monotonic) -> None:
        self.limites = limites
        self._reloj = reloj
        self.en_curso: dict[str, int] = dict.fromkeys(CLASES, 0)
        self._colas: dict[str, deque[_Espera]] = {clase: deque() for clase in CLASES}

    def configurar(self, limites: Limites) -> None:
        self.limites = limites
        self._despachar()

    def estado(self) -> dict:
        return {
            "en_curso": dict(self.en_curso),
            "en_espera": {clase: sum(1 for e in cola if not e.futuro.done()) for clase, cola in self._colas.items()},
            "limites": self.limites,
        }

    def _cabe(self, clase: str) -> bool:
        limites = self.limites
        if sum(self.en_curso.values()) >= limites.total:
            return False
        if clase == CHAT:
            return True
        if self.en_curso[CRON] + self.en_curso[SEGUNDO_PLANO] >= limites.no_interactivo:
            return False
        return clase == CRON or self.en_curso[SEGUNDO_PLANO] < limites.segundo_plano

    def cabe(self, clase: str) -> bool:
        return self._cabe(clase)

    def ocupar(self, clase: str) -> None:
        self.en_curso[clase] += 1

    def _primero(self, clase: str) -> _Espera | None:
        cola = self._colas[clase]
        while cola and cola[0].futuro.done():
            cola.popleft()
        return cola[0] if cola else None

    def _elegir(self) -> _Espera | None:
        candidatos = {clase: self._primero(clase) if self._cabe(clase) else None for clase in CLASES}
        chat, cron, fondo = candidatos[CHAT], candidatos[CRON], candidatos[SEGUNDO_PLANO]
        ventaja = self.limites.envejecimiento_s
        no_interactivo = cron or fondo
        if cron and fondo and fondo.llegada + ventaja <= cron.llegada:
            no_interactivo = fondo
        if chat and no_interactivo:
            adelanto = ventaja if self.limites.chat_preferente else 0.0
            return chat if chat.llegada - adelanto <= no_interactivo.llegada else no_interactivo
        return chat or no_interactivo

    def _despachar(self) -> None:
        while (espera := self._elegir()) is not None:
            self._colas[espera.clase].popleft()
            self.en_curso[espera.clase] += 1
            espera.futuro.set_result(None)

    async def adquirir(self, clase: str) -> None:
        espera = _Espera(clase, self._reloj(), asyncio.get_running_loop().create_future())
        self._colas[clase].append(espera)
        self._despachar()
        try:
            await espera.futuro
        except BaseException:
            if espera.futuro.done() and not espera.futuro.cancelled():
                self.liberar(clase)
            else:
                espera.futuro.cancel()
                with contextlib.suppress(ValueError):
                    self._colas[clase].remove(espera)
                self._despachar()
            raise

    def liberar(self, clase: str) -> None:
        if self.en_curso[clase] <= 0:
            logger.error("Liberación de un cupo %s que no estaba tomado", clase)
            return
        self.en_curso[clase] -= 1
        self._despachar()
