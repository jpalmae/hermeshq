from fastapi import APIRouter, Depends
from sqlalchemy import false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_accessible_agent_ids, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.task import Task
from hermeshq.models.user import User

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _active_agent_clause():
    return Agent.is_archived.is_(False)


@router.get("/overview")
async def overview(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    accessible_ids = await get_accessible_agent_ids(db, current_user)
    agent_scope = Agent.id.in_(accessible_ids) if accessible_ids else false()
    task_scope = Task.agent_id.in_(accessible_ids) if accessible_ids else false()
    activity_scope = ActivityLog.agent_id.in_(accessible_ids) if accessible_ids else false()
    total_agents = await db.scalar(
        select(func.count()).select_from(Agent).where(_active_agent_clause(), agent_scope)
        if not is_admin(current_user)
        else select(func.count()).select_from(Agent).where(_active_agent_clause())
    )
    active_agents = await db.scalar(
        select(func.count()).select_from(Agent).where(Agent.status == "running", _active_agent_clause(), agent_scope)
        if not is_admin(current_user)
        else select(func.count()).select_from(Agent).where(Agent.status == "running", _active_agent_clause())
    )
    total_tasks = await db.scalar(
        select(func.count()).select_from(Task).where(task_scope) if not is_admin(current_user) else select(func.count()).select_from(Task)
    )
    queued_tasks = await db.scalar(
        select(func.count()).select_from(Task).where(Task.status == "queued", task_scope)
        if not is_admin(current_user)
        else select(func.count()).select_from(Task).where(Task.status == "queued")
    )
    recent_activity_statement = select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(12)
    if not is_admin(current_user):
        recent_activity_statement = recent_activity_statement.where(activity_scope)
    recent_activity = (await db.execute(recent_activity_statement)).scalars().all()
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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    statement = select(Agent).where(_active_agent_clause()).order_by(Agent.created_at.asc())
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(Agent.id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    result = await db.execute(statement)
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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    statement = select(Agent).where(_active_agent_clause()).order_by(Agent.total_tokens_used.desc())
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(Agent.id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    result = await db.execute(statement)
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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    statement = select(Task)
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(Task.agent_id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    all_tasks = (await db.execute(statement)).scalars().all()
    counts: dict[str, int] = {}
    for task in all_tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    return {"counts": counts, "total": len(all_tasks)}


@router.get("/activity")
async def activity(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    statement = select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(50)
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(ActivityLog.agent_id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    result = await db.execute(statement)
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
