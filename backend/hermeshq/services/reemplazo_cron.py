from __future__ import annotations

import logging

from sqlalchemy import select

from hermeshq.models.activity import ActivityLog
from hermeshq.models.scheduled_task import ScheduledTask

logger = logging.getLogger(__name__)

EVENTO_PEDIDO = "schedule.replacement_requested"
EVENTO_HECHO = "schedule.replaced"


async def anterior_valido(db, agent_id: str, owner_user_id: str, schedule_id: str) -> ScheduledTask | None:
    anterior = await db.get(ScheduledTask, (schedule_id or "").strip())
    if (
        not isinstance(anterior, ScheduledTask)
        or anterior.agent_id != agent_id
        or str(anterior.owner_user_id or "") != str(owner_user_id or "")
        or anterior.created_by_agent_id != agent_id
    ):
        return None
    return anterior


def registrar_pedido(db, agent_id: str, nueva_id: str, anterior: ScheduledTask) -> None:
    db.add(
        ActivityLog(
            agent_id=agent_id,
            event_type=EVENTO_PEDIDO,
            message=anterior.name,
            details={"schedule_id": nueva_id, "reemplaza": anterior.id},
        )
    )


async def aplicar_reemplazo(db, nueva: ScheduledTask, aprobado_por: str | None) -> str | None:
    if not getattr(nueva, "agent_id", None) or not getattr(nueva, "id", None):
        return None
    try:
        return await _aplicar(db, nueva, aprobado_por)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo aplicar el reemplazo del cron %s", getattr(nueva, "id", None))
        return None


async def _aplicar(db, nueva: ScheduledTask, aprobado_por: str | None) -> str | None:
    resultado = await db.execute(
        select(ActivityLog)
        .where(ActivityLog.event_type == EVENTO_PEDIDO, ActivityLog.agent_id == nueva.agent_id)
        .order_by(ActivityLog.created_at.desc())
    )
    pedido = next(
        (
            fila
            for fila in resultado.scalars().all()
            if isinstance(fila.details, dict) and fila.details.get("schedule_id") == nueva.id
        ),
        None,
    )
    if pedido is None:
        return None
    anterior = await anterior_valido(db, nueva.agent_id, nueva.owner_user_id, pedido.details.get("reemplaza"))
    if anterior is None or anterior.id == nueva.id:
        return None
    db.add(
        ActivityLog(
            agent_id=nueva.agent_id,
            event_type=EVENTO_HECHO,
            message=anterior.name,
            details={
                "schedule_id": anterior.id,
                "reemplazada_por": nueva.id,
                "aprobado_por": aprobado_por,
                "definicion": {
                    "name": anterior.name,
                    "cron_expression": anterior.cron_expression,
                    "prompt": anterior.prompt,
                    "output_format": anterior.output_format,
                    "timezone": anterior.timezone,
                    "enabled": anterior.enabled,
                },
            },
        )
    )
    nombre = anterior.name
    await _pasar_historial(db, anterior, nueva)
    await db.delete(anterior)
    return nombre


MAX_EJECUCIONES_MIGRADAS = 500


async def _pasar_historial(db, anterior: ScheduledTask, nueva: ScheduledTask) -> None:
    try:
        from hermeshq.models.task import Task

        resultado = await db.execute(
            select(Task)
            .where(
                Task.agent_id == nueva.agent_id,
                Task.metadata_json["scheduled_task_id"].as_string() == anterior.id,
            )
            .limit(MAX_EJECUCIONES_MIGRADAS)
        )
        for ejecucion in resultado.scalars().all():
            if isinstance(getattr(ejecucion, "metadata_json", None), dict):
                ejecucion.metadata_json = {
                    **ejecucion.metadata_json,
                    "scheduled_task_id": nueva.id,
                    "scheduled_task_id_anterior": anterior.id,
                }
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo pasar el historial del cron %s a %s", anterior.id, nueva.id)
