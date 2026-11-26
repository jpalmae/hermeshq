import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_accessible_agent_ids, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.user import User
from hermeshq.schemas.scheduled_task import (
    ScheduledTaskApprove,
    ScheduledTaskCreate,
    ScheduledTaskDelivery,
    ScheduledTaskRead,
    ScheduledTaskUpdate,
)
from hermeshq.services.cron_schedule import (
    CronScheduleError,
    build_scheduled_prompt,
    cambia_la_definicion,
    compute_next_run,
    scheduled_task_metadata,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scheduled-tasks", tags=["scheduled-tasks"])


def _compute_next_run(expression: str, timezone_name: str | None) -> datetime:
    try:
        return compute_next_run(expression, timezone_name)
    except CronScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _ensure_can_manage(item: ScheduledTask, current_user: User) -> None:
    if is_admin(current_user):
        return
    if not item.owner_user_id or item.owner_user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Solo quien creó esta programación o un administrador puede modificarla.",
        )


def _requires_reapproval(item: ScheduledTask, changes: dict, current_user: User) -> bool:
    if is_admin(current_user) or item.approval_status != "approved":
        return False
    if "prompt" in changes and changes["prompt"] is not None and changes["prompt"] != item.prompt:
        return True
    if "output_format" in changes and (changes["output_format"] or None) != (
        getattr(item, "output_format", None) or None
    ):
        return True
    expression = changes.get("cron_expression")
    return expression is not None and " ".join(expression.split()) != " ".join(item.cron_expression.split())


def _evento_de_actualizacion(campos: list[str], enabled: bool) -> str:
    if campos == ["enabled"]:
        return "schedule.enabled" if enabled else "schedule.disabled"
    return "schedule.updated"


async def create_scheduled_task_record(
    db: AsyncSession,
    payload: ScheduledTaskCreate,
    owner_user_id: str | None,
) -> ScheduledTask:
    item = ScheduledTask(
        **payload.model_dump(),
        owner_user_id=owner_user_id,
        next_run=_compute_next_run(payload.cron_expression, payload.timezone),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


@router.get("", response_model=list[ScheduledTaskRead])
async def list_scheduled_tasks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[ScheduledTaskRead]:
    statement = select(ScheduledTask).order_by(ScheduledTask.created_at.asc())
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = (
            statement.where(ScheduledTask.agent_id.in_(accessible_ids), ScheduledTask.owner_user_id == current_user.id)
            if accessible_ids
            else statement.where(false())
        )
    result = await db.execute(statement)
    return [ScheduledTaskRead.model_validate(item) for item in result.scalars().all()]


@router.post("", response_model=ScheduledTaskRead, status_code=status.HTTP_201_CREATED)
async def create_scheduled_task(
    payload: ScheduledTaskCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledTaskRead:
    await ensure_agent_access(db, current_user, payload.agent_id)
    item = await create_scheduled_task_record(db, payload, current_user.id)
    return ScheduledTaskRead.model_validate(item)


@router.put("/{scheduled_task_id}", response_model=ScheduledTaskRead)
async def update_scheduled_task(
    scheduled_task_id: str,
    payload: ScheduledTaskUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledTaskRead:
    item = await db.get(ScheduledTask, scheduled_task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    await ensure_agent_access(db, current_user, item.agent_id)
    _ensure_can_manage(item, current_user)
    changes = payload.model_dump(exclude_unset=True)
    reapproval = _requires_reapproval(item, changes, current_user)
    if item.approval_status == "pending" and changes.get("enabled"):
        raise HTTPException(status_code=400, detail="This schedule is pending approval; approve it first")
    if "timezone" in changes and changes["timezone"] is None:
        changes.pop("timezone")
    new_expression = changes.get("cron_expression") or item.cron_expression
    new_timezone = changes.get("timezone") or item.timezone
    next_run = (
        _compute_next_run(new_expression, new_timezone)
        if "cron_expression" in changes or "timezone" in changes
        else None
    )
    nueva_version = cambia_la_definicion(item, changes)
    campos = sorted(field for field, value in changes.items() if getattr(item, field, None) != value)
    for field, value in changes.items():
        setattr(item, field, value)
    if nueva_version:
        item.definition_version = int(item.definition_version or 1) + 1
    if next_run:
        item.next_run = next_run
    if reapproval:
        item.approval_status = "pending"
        item.approval_code = None
        item.enabled = False
    if campos or reapproval:
        db.add(
            ActivityLog(
                agent_id=item.agent_id,
                event_type=_evento_de_actualizacion(campos, bool(item.enabled)),
                message=item.name,
                details={
                    "schedule_id": item.id,
                    "actor_user_id": current_user.id,
                    "channel": "web",
                    "fields": campos,
                    "requires_reapproval": reapproval,
                    "definition_version": item.definition_version,
                },
            )
        )
    await db.commit()
    await db.refresh(item)
    return ScheduledTaskRead.model_validate(item)


@router.patch("/{scheduled_task_id}", response_model=ScheduledTaskRead)
async def update_scheduled_task_delivery(
    scheduled_task_id: str,
    payload: ScheduledTaskDelivery,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledTaskRead:
    from hermeshq.models.public_chat import PublicChatApiKey

    item = await db.get(ScheduledTask, scheduled_task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    await ensure_agent_access(db, current_user, item.agent_id)
    _ensure_can_manage(item, current_user)
    clave_id = (payload.delivery_key_id or "").strip() or None
    if clave_id:
        clave = await db.get(PublicChatApiKey, clave_id)
        if (
            clave is None
            or not clave.is_active
            or clave.agent_id != item.agent_id
            or not item.owner_user_id
            or clave.bound_user_id != item.owner_user_id
        ):
            raise HTTPException(
                status_code=422,
                detail="La clave debe estar activa, ser del mismo agente y estar fija al dueño de la programación.",
            )
    anterior = item.delivery_key_id
    item.delivery_key_id = clave_id
    if anterior != clave_id:
        db.add(
            ActivityLog(
                agent_id=item.agent_id,
                event_type="schedule.delivery_changed",
                message=item.name,
                details={
                    "schedule_id": item.id,
                    "actor_user_id": current_user.id,
                    "delivery_key_id": clave_id,
                    "previous_delivery_key_id": anterior,
                },
            )
        )
    await db.commit()
    await db.refresh(item)
    return ScheduledTaskRead.model_validate(item)


@router.post("/{scheduled_task_id}/run", response_model=ScheduledTaskRead, status_code=status.HTTP_202_ACCEPTED)
async def run_scheduled_task_now(
    scheduled_task_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledTaskRead:
    from hermeshq.models.task import Task

    item = await db.get(ScheduledTask, scheduled_task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    await ensure_agent_access(db, current_user, item.agent_id)
    _ensure_can_manage(item, current_user)
    if item.approval_status == "pending":
        raise HTTPException(status_code=400, detail="This schedule is pending approval; approve it first")

    from hermeshq.models.agent import Agent

    agente = await db.get(Agent, item.agent_id)
    task = Task(
        agent_id=item.agent_id,
        title=item.name,
        prompt=build_scheduled_prompt(item),
        created_by_user_id=item.owner_user_id,
        metadata_json=scheduled_task_metadata(
            item, manual=True, workspace_path=getattr(agente, "workspace_path", None)
        ),
    )
    db.add(task)
    await db.flush()
    item.last_run = datetime.now(UTC)
    db.add(
        ActivityLog(
            agent_id=item.agent_id,
            task_id=task.id,
            event_type="schedule.triggered",
            message=f"{item.name} (manual)",
            details={"schedule_id": item.id, "manual": True},
        )
    )
    await db.commit()
    await db.refresh(item)

    supervisor = getattr(request.app.state, "supervisor", None)
    if supervisor:
        await supervisor.submit_task(task.id)

    return ScheduledTaskRead.model_validate(item)


@router.post("/{scheduled_task_id}/approve", response_model=ScheduledTaskRead)
async def approve_scheduled_task(
    scheduled_task_id: str,
    payload: ScheduledTaskApprove,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledTaskRead:
    item = await db.get(ScheduledTask, scheduled_task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    await ensure_agent_access(db, current_user, item.agent_id)
    if item.approval_status != "pending":
        raise HTTPException(status_code=409, detail="Schedule is not pending approval")
    if item.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the user who requested this schedule can approve it")
    formato = getattr(item, "output_format", None) or None
    if formato and payload.output_format is None and payload.prompt == item.prompt:
        raise HTTPException(
            status_code=409,
            detail="Esta programación tiene un formato de salida que esta pantalla no muestra: apruébala desde el chat "
            "con «aprobar CÓDIGO», donde ves el prompt y el formato completos.",
        )
    if (
        payload.prompt != item.prompt
        or " ".join(payload.cron_expression.split()) != item.cron_expression
        or (payload.output_format or None) != formato
    ):
        raise HTTPException(
            status_code=409, detail="The schedule changed after you reviewed it; reload and review again"
        )
    item.approval_status = "approved"
    item.approval_code = None
    item.enabled = True
    item.next_run = _compute_next_run(item.cron_expression, item.timezone)
    from hermeshq.services.reemplazo_cron import aplicar_reemplazo

    await aplicar_reemplazo(db, item, current_user.id)
    db.add(
        ActivityLog(
            agent_id=item.agent_id,
            event_type="schedule.approved",
            message=item.name,
            details={"schedule_id": item.id, "approved_by": current_user.id},
        )
    )
    await db.commit()
    await db.refresh(item)
    return ScheduledTaskRead.model_validate(item)


@router.delete("/{scheduled_task_id}", status_code=204)
async def delete_scheduled_task(
    scheduled_task_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    item = await db.get(ScheduledTask, scheduled_task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    await ensure_agent_access(db, current_user, item.agent_id)
    _ensure_can_manage(item, current_user)
    db.add(
        ActivityLog(
            agent_id=item.agent_id,
            event_type="schedule.deleted",
            message=item.name,
            details={
                "schedule_id": item.id,
                "actor_user_id": current_user.id,
                "channel": "web",
                "owner_user_id": item.owner_user_id,
                "approval_status": item.approval_status,
            },
        )
    )
    await db.delete(item)
    await db.commit()
