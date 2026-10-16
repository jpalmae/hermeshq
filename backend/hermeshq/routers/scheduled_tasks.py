import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_accessible_agent_ids, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.user import User
from hermeshq.schemas.scheduled_task import (
    ScheduledTaskApprove,
    ScheduledTaskCreate,
    ScheduledTaskRead,
    ScheduledTaskUpdate,
)
from hermeshq.services.cron_schedule import (
    CronScheduleError,
    build_scheduled_prompt,
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
            statement.where(ScheduledTask.agent_id.in_(accessible_ids)) if accessible_ids else statement.where(false())
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
    changes = payload.model_dump(exclude_unset=True)
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
    for field, value in changes.items():
        setattr(item, field, value)
    if next_run:
        item.next_run = next_run
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
    from hermeshq.models.activity import ActivityLog
    from hermeshq.models.task import Task

    item = await db.get(ScheduledTask, scheduled_task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    await ensure_agent_access(db, current_user, item.agent_id)
    if item.approval_status == "pending":
        raise HTTPException(status_code=400, detail="This schedule is pending approval; approve it first")

    task = Task(
        agent_id=item.agent_id,
        title=item.name,
        prompt=build_scheduled_prompt(item),
        created_by_user_id=item.owner_user_id,
        metadata_json=scheduled_task_metadata(item, manual=True),
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
    from hermeshq.models.activity import ActivityLog

    item = await db.get(ScheduledTask, scheduled_task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    await ensure_agent_access(db, current_user, item.agent_id)
    if item.approval_status != "pending":
        raise HTTPException(status_code=409, detail="Schedule is not pending approval")
    if item.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the user who requested this schedule can approve it")
    if payload.prompt != item.prompt or " ".join(payload.cron_expression.split()) != item.cron_expression:
        raise HTTPException(
            status_code=409, detail="The schedule changed after you reviewed it; reload and review again"
        )
    item.approval_status = "approved"
    item.approval_code = None
    item.enabled = True
    item.next_run = _compute_next_run(item.cron_expression, item.timezone)
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
    await db.delete(item)
    await db.commit()
