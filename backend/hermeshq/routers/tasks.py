from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.schemas.task import TaskCreate, TaskRead

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[TaskRead]:
    result = await db.execute(select(Task).order_by(desc(Task.queued_at)))
    return [TaskRead.model_validate(task) for task in result.scalars().all()]


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    request: Request,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TaskRead:
    agent = await db.get(Agent, payload.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    task = Task(**payload.model_dump())
    db.add(task)
    await db.commit()
    await db.refresh(task)
    if agent.status == "running":
        await request.app.state.supervisor.submit_task(task.id)
    return TaskRead.model_validate(task)


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: str,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TaskRead:
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskRead.model_validate(task)


@router.post("/{task_id}/cancel", response_model=TaskRead)
async def cancel_task(
    task_id: str,
    request: Request,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TaskRead:
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await request.app.state.supervisor.cancel_task(task_id)
    await db.refresh(task)
    return TaskRead.model_validate(task)


@router.get("/queue/state")
async def queue_state(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    queued_result = await db.execute(select(Task).where(Task.status == "queued"))
    running_result = await db.execute(select(Task).where(Task.status == "running"))
    return {
        "queued": len(queued_result.scalars().all()),
        "running": len(running_result.scalars().all()),
    }
