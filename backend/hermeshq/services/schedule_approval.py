import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.activity import ActivityLog
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.services.cron_schedule import (
    APPROVAL_CODE_TTL,
    DEFAULT_TIMEZONE,
    codigo_eliminacion,
    compute_next_run,
    resolve_timezone,
)

APPROVAL_COMMAND = re.compile(r"^\s*aprobar\s+([A-Za-z0-9]{4,12})\s*[.!]?\s*$", re.IGNORECASE)
DELETE_COMMAND = re.compile(r"^\s*eliminar\s+([A-Za-z0-9]{4,12})\s*[.!]?\s*$", re.IGNORECASE)


def deletion_code_from(text: str | None) -> str | None:
    match = DELETE_COMMAND.match(text or "")
    return match.group(1).upper() if match else None


async def delete_by_code(db: AsyncSession, agent_id: str, user_id: str, code: str, channel: str) -> str | None:
    result = await db.execute(
        select(ScheduledTask).where(
            ScheduledTask.agent_id == agent_id,
            ScheduledTask.owner_user_id == user_id,
            ScheduledTask.approval_status == "approved",
        )
    )
    propios = list(result.scalars().all())
    if not propios:
        return None
    match = next((item for item in propios if codigo_eliminacion(item).upper() == code.upper()), None)
    if match is None:
        return (
            f"⚠️ El código {code} no corresponde a ninguna de tus programaciones, así que no eliminé nada. "
            "Pídeme de nuevo que la elimine para recibir el código correcto."
        )
    nombre = match.name
    db.add(
        ActivityLog(
            agent_id=agent_id,
            event_type="schedule.deleted_by_agent",
            message=nombre,
            details={"schedule_id": match.id, "deleted_by": user_id, "channel": channel, "by": "chat_command"},
        )
    )
    await db.delete(match)
    await db.commit()
    return f"🗑️ Eliminé la programación «{nombre}». Ya no se ejecutará."


def approval_code_from(text: str | None) -> str | None:
    match = APPROVAL_COMMAND.match(text or "")
    return match.group(1).upper() if match else None


def _local(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(resolve_timezone(DEFAULT_TIMEZONE)).strftime("%Y-%m-%d %H:%M")


def _is_expired(item: ScheduledTask) -> bool:
    created = item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - created > APPROVAL_CODE_TTL


async def approve_by_code(db: AsyncSession, agent_id: str, user_id: str, code: str, channel: str) -> str | None:
    result = await db.execute(
        select(ScheduledTask).where(
            ScheduledTask.agent_id == agent_id,
            ScheduledTask.owner_user_id == user_id,
            ScheduledTask.approval_status == "pending",
        )
    )
    pending = [item for item in result.scalars().all() if item.approval_code and not _is_expired(item)]
    if not pending:
        return None
    match = next((item for item in pending if item.approval_code.upper() == code.upper()), None)
    if match is None:
        options = "; ".join(f"«{item.name}» → `aprobar {item.approval_code}`" for item in pending[:5])
        return f"⚠️ No reconozco el código {code}. Tienes pendiente: {options}"
    match.approval_status = "approved"
    match.enabled = True
    match.approval_code = None
    match.next_run = compute_next_run(match.cron_expression, match.timezone)
    from hermeshq.services.reemplazo_cron import aplicar_reemplazo

    reemplazada = await aplicar_reemplazo(db, match, user_id)
    db.add(
        ActivityLog(
            agent_id=agent_id,
            event_type="schedule.approved",
            message=match.name,
            details={"schedule_id": match.id, "approved_by": user_id, "channel": channel, "by": "chat_command"},
        )
    )
    await db.commit()
    proximo = _local(match.next_run)
    return (
        f"✅ Activada la programación «{match.name}»."
        + (f" Reemplaza a «{reemplazada}», que quedó eliminada." if reemplazada else "")
        + (f" Primera ejecución: {proximo} (hora de Chile)." if proximo else "")
    )
