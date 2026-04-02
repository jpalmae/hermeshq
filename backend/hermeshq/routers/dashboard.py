from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.task import Task
from hermeshq.models.user import User

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/overview")
async def overview(
    request: Request,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    total_agents = await db.scalar(select(func.count()).select_from(Agent))
    active_agents = await db.scalar(select(func.count()).select_from(Agent).where(Agent.status == "running"))
    total_tasks = await db.scalar(select(func.count()).select_from(Task))
    queued_tasks = await db.scalar(select(func.count()).select_from(Task).where(Task.status == "queued"))
    recent_activity = await request.app.state.supervisor.get_recent_activity(limit=12)
    return {
        "stats": {
            "total_agents": total_agents or 0,
            "active_agents": active_agents or 0,
            "total_tasks": total_tasks or 0,
            "queued_tasks": queued_tasks or 0,
        },
        "activity": [
            {
                "id": item.id,
                "event_type": item.event_type,
                "message": item.message,
                "severity": item.severity,
                "created_at": item.created_at,
            }
            for item in recent_activity
        ],
    }


@router.get("/agents")
async def agents_summary(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    result = await db.execute(select(Agent).order_by(Agent.created_at.asc()))
    return [
        {
            "id": agent.id,
            "name": agent.name,
            "slug": agent.slug,
            "status": agent.status,
            "model": agent.model,
            "tokens": agent.total_tokens_used,
            "tasks": agent.total_tasks,
            "last_activity": agent.last_activity,
        }
        for agent in result.scalars().all()
    ]


@router.get("/tokens")
async def token_stats(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    result = await db.execute(select(Agent).order_by(Agent.total_tokens_used.desc()))
    agents = result.scalars().all()
    return {
        "total_tokens": sum(agent.total_tokens_used for agent in agents),
        "by_agent": [
            {"agent_id": agent.id, "name": agent.name, "tokens": agent.total_tokens_used}
            for agent in agents
        ],
    }


@router.get("/tasks/stats")
async def task_stats(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    all_tasks = (await db.execute(select(Task))).scalars().all()
    counts: dict[str, int] = {}
    for task in all_tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    return {"counts": counts, "total": len(all_tasks)}


@router.get("/activity")
async def activity(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    result = await db.execute(select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(50))
    return [
        {
            "id": item.id,
            "event_type": item.event_type,
            "message": item.message,
            "severity": item.severity,
            "created_at": item.created_at,
        }
        for item in result.scalars().all()
    ]
