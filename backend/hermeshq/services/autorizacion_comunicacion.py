from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.models.agent import Agent
from hermeshq.services.delegacion_entre_usuarios import duenos_distintos, duenos_humanos_por_agente

logger = logging.getLogger(__name__)

OPERACIONES = frozenset({"direct", "delegate", "broadcast"})


def _fecha(valor) -> datetime | None:
    if not isinstance(valor, str):
        return None
    try:
        fecha = datetime.fromisoformat(valor)
    except ValueError:
        return None
    return fecha if fecha.tzinfo else None


def evaluar_excepcion(
    excepciones: list[dict] | None, origen: Agent, destino: Agent, operacion: str, ahora: datetime | None = None
) -> str | None:
    if operacion not in OPERACIONES:
        return None
    ahora = ahora or datetime.now(UTC)
    for exc in excepciones or []:
        if not isinstance(exc, dict) or exc.get("revocada") is True:
            continue
        if exc.get("origen") != origen.id or exc.get("destino") != destino.id:
            continue
        operaciones = exc.get("operaciones")
        if not isinstance(operaciones, list) or operacion not in operaciones:
            continue
        expira = _fecha(exc.get("expira"))
        if expira is None or ahora >= expira:
            continue
        if not str(exc.get("aprobada_por") or "").strip():
            continue
        return str(exc.get("id") or "sin-id")
    return None


def exigir_con_duenos(
    duenos: dict[str, set[str]], origen: Agent, destino: Agent, mensaje: str, operacion: str = "direct"
) -> None:
    if origen.id == destino.id:
        return
    if not duenos_distintos(duenos, origen, destino):
        return
    exc_id = evaluar_excepcion(get_settings().cross_user_delegation_exceptions, origen, destino, operacion)
    if exc_id:
        logger.warning("Excepcion %s usada para %s: %s -> %s", exc_id, operacion, origen.id, destino.id)
        return
    logger.warning("Comunicacion entre propietarios denegada (%s): %s -> %s", operacion, origen.id, destino.id)
    raise HTTPException(status_code=403, detail=mensaje)


async def exigir_mismo_propietario(
    db: AsyncSession, origen: Agent, destino: Agent, mensaje: str, operacion: str = "direct"
) -> None:
    exigir_con_duenos(await duenos_humanos_por_agente(db), origen, destino, mensaje, operacion)
