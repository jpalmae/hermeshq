from datetime import datetime, timezone

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.user import User
from hermeshq.schemas.scheduled_task import (
    ScheduledTaskCreate,
    ScheduledTaskRead,
    ScheduledTaskUpdate,
)

router = APIRouter(prefix="/scheduled-tasks", tags=["scheduled-tasks"])


def _compute_next_run(expression: str) -> datetime:
    now = datetime.now(timezone.utc)
    if len(expression.split()) == 6:
        return croniter(expression, now, second_at_beginning=True).get_next(datetime)
    return croniter(expression, now).get_next(datetime)


@router.get("", response_model=list[ScheduledTaskRead])
async def list_scheduled_tasks(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[ScheduledTaskRead]:
    result = await db.execute(select(ScheduledTask).order_by(ScheduledTask.created_at.asc()))
    return [ScheduledTaskRead.model_validate(item) for item in result.scalars().all()]


@router.post("", response_model=ScheduledTaskRead)
async def create_scheduled_task(
    payload: ScheduledTaskCreate,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledTaskRead:
    item = ScheduledTask(**payload.model_dump(), next_run=_compute_next_run(payload.cron_expression))
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return ScheduledTaskRead.model_validate(item)


@router.put("/{scheduled_task_id}", response_model=ScheduledTaskRead)
async def update_scheduled_task(
    scheduled_task_id: str,
    payload: ScheduledTaskUpdate,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledTaskRead:
    item = await db.get(ScheduledTask, scheduled_task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    if payload.cron_expression:
        item.next_run = _compute_next_run(payload.cron_expression)
    await db.commit()
    await db.refresh(item)
    return ScheduledTaskRead.model_validate(item)


@router.delete("/{scheduled_task_id}", status_code=204)
async def delete_scheduled_task(
    scheduled_task_id: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    item = await db.get(ScheduledTask, scheduled_task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    await db.delete(item)
    await db.commit()
