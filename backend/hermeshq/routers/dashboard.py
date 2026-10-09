import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_accessible_agent_ids, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.schemas.dashboard import (
    DashboardActivityItemRead,
    DashboardAgentSummaryRead,
    DashboardAnalyticsRead,
    DashboardChannelRead,
    DashboardFleetHealthRead,
    DashboardOverviewRead,
    DashboardTaskStatsRead,
    DashboardTokenStatsRead,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _active_agent_clause():
    return Agent.is_archived.is_(False)


@router.get("/overview", response_model=DashboardOverviewRead)
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
        select(func.count()).select_from(Task).where(task_scope)
        if not is_admin(current_user)
        else select(func.count()).select_from(Task)
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


@router.get("/agents", response_model=list[DashboardAgentSummaryRead])
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


@router.get("/tokens", response_model=DashboardTokenStatsRead)
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
        "by_agent": [{"agent_id": agent.id, "name": agent.name, "tokens": agent.total_tokens_used} for agent in agents],
    }


@router.get("/tasks/stats", response_model=DashboardTaskStatsRead)
async def task_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    statement = select(Task.status, func.count()).group_by(Task.status)
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(Task.agent_id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    rows = (await db.execute(statement)).all()
    counts: dict[str, int] = {status: count for status, count in rows}
    return {"counts": counts, "total": sum(counts.values())}


@router.get("/activity", response_model=list[DashboardActivityItemRead])
async def activity(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    statement = select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(50)
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = (
            statement.where(ActivityLog.agent_id.in_(accessible_ids)) if accessible_ids else statement.where(false())
        )
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


@router.get("/channels", response_model=list[DashboardChannelRead])
async def channels_overview(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    statement = (
        select(MessagingChannel, Agent)
        .join(Agent, MessagingChannel.agent_id == Agent.id)
        .order_by(Agent.name.asc(), MessagingChannel.platform.asc())
    )
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(Agent.id.in_(accessible_ids)) if accessible_ids else statement.where(false())

    rows = (await db.execute(statement)).all()
    channels: list[dict] = []
    for channel, agent in rows:
        meta = channel.metadata_json or {}
        paired_at_str = meta.get("connected_at") or meta.get("whatsapp_paired_at")
        paired_at: datetime | None = None
        days_since_paired: int | None = None
        if paired_at_str:
            try:
                paired_at = datetime.fromisoformat(paired_at_str)
                days_since_paired = (datetime.now(UTC) - paired_at).days
            except (ValueError, TypeError):
                pass
        channels.append(
            {
                "agent_id": agent.id,
                "agent_name": agent.name,
                "agent_slug": agent.slug,
                "platform": channel.platform,
                "enabled": channel.enabled,
                "status": channel.status,
                "paired_at": paired_at.isoformat() if paired_at else None,
                "days_since_paired": days_since_paired,
            }
        )
    return channels


@router.get("/health", response_model=DashboardFleetHealthRead)
async def fleet_health(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Fleet-wide health: agent status breakdown, task outcomes, recent errors."""
    accessible_ids = await get_accessible_agent_ids(db, current_user)
    agent_scope = Agent.id.in_(accessible_ids) if accessible_ids else false()
    task_scope = Task.agent_id.in_(accessible_ids) if accessible_ids else false()
    activity_scope = ActivityLog.agent_id.in_(accessible_ids) if accessible_ids else false()

    # Agent status breakdown
    by_status = await db.execute(
        select(Agent.status, func.count()).where(Agent.is_archived.is_(False), agent_scope).group_by(Agent.status)
    )
    status_breakdown = dict(by_status.all())

    # Task outcome summary
    task_outcomes = await db.execute(select(Task.status, func.count()).where(task_scope).group_by(Task.status))
    task_summary = dict(task_outcomes.all())

    # Recent errors (last 24h)
    since = datetime.now(UTC) - timedelta(hours=24)
    error_statement = (
        select(
            ActivityLog.agent_id,
            ActivityLog.message,
            ActivityLog.created_at,
        )
        .where(
            ActivityLog.severity == "error",
            ActivityLog.created_at >= since,
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(10)
    )
    if not is_admin(current_user):
        error_statement = error_statement.where(activity_scope)
    error_rows = (await db.execute(error_statement)).all()

    # Resolve agent names
    error_agent_ids = {r[0] for r in error_rows if r[0]}
    agent_names: dict = {}
    if error_agent_ids:
        name_rows = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(error_agent_ids)))
        agent_names = dict(name_rows.all())

    return {
        "status_breakdown": status_breakdown,
        "task_summary": task_summary,
        "recent_errors": [
            {
                "agent_id": r[0],
                "agent_name": agent_names.get(r[0], "Unknown"),
                "message": r[1],
                "timestamp": r[2].isoformat() if r[2] else None,
            }
            for r in error_rows
        ],
        "last_updated": datetime.now(UTC).isoformat(),
    }


@router.get("/analytics", response_model=DashboardAnalyticsRead)
async def task_analytics(
    days: int = 14,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Task analytics: time-series, completion metrics, top failing agents."""
    accessible_ids = await get_accessible_agent_ids(db, current_user)
    task_scope = Task.agent_id.in_(accessible_ids) if accessible_ids else false()

    days = max(1, min(days, 90))  # clamp 1-90

    since = datetime.now(UTC) - timedelta(days=days)

    # --- Daily task counts by status ---
    daily_rows = (
        await db.execute(
            select(
                func.date_trunc("day", Task.queued_at).label("day"),
                Task.status,
                func.count().label("cnt"),
            )
            .where(Task.queued_at >= since, task_scope)
            .group_by("day", Task.status)
            .order_by("day")
        )
    ).all()

    # Build time-series: { "2026-05-20": { "completed": 12, "failed": 2, ... } }
    time_series: dict[str, dict[str, int]] = {}
    for row in daily_rows:
        day_key = row[0].strftime("%Y-%m-%d") if row[0] else "unknown"
        if day_key not in time_series:
            time_series[day_key] = {}
        time_series[day_key][row[1]] = row[2]

    # --- Completion metrics (only completed tasks) ---
    completed_tasks = (
        await db.execute(
            select(
                func.avg(func.extract("epoch", Task.completed_at - Task.started_at)).label("avg_seconds"),
            ).where(
                Task.status == "completed",
                Task.started_at.isnot(None),
                Task.completed_at.isnot(None),
                Task.completed_at >= since,
                task_scope,
            )
        )
    ).one()

    avg_seconds = float(completed_tasks[0] or 0)

    # --- P50 and P95 ---
    p50_row = (
        await db.execute(
            select(
                func.percentile_cont(0.5).within_group(func.extract("epoch", Task.completed_at - Task.started_at))
            ).where(
                Task.status == "completed",
                Task.started_at.isnot(None),
                Task.completed_at.isnot(None),
                Task.completed_at >= since,
                task_scope,
            )
        )
    ).scalar()

    p95_row = (
        await db.execute(
            select(
                func.percentile_cont(0.95).within_group(func.extract("epoch", Task.completed_at - Task.started_at))
            ).where(
                Task.status == "completed",
                Task.started_at.isnot(None),
                Task.completed_at.isnot(None),
                Task.completed_at >= since,
                task_scope,
            )
        )
    ).scalar()

    # --- Total counts for success rate ---
    total_in_period = (
        await db.scalar(select(func.count()).select_from(Task).where(Task.queued_at >= since, task_scope)) or 0
    )
    failed_in_period = (
        await db.scalar(
            select(func.count()).select_from(Task).where(Task.status == "failed", Task.queued_at >= since, task_scope)
        )
        or 0
    )

    success_rate = ((total_in_period - failed_in_period) / total_in_period * 100) if total_in_period > 0 else 100.0

    # --- Top failing agents (last 7 days) ---
    seven_days_ago = datetime.now(UTC) - timedelta(days=7)
    top_fail_rows = (
        await db.execute(
            select(
                Task.agent_id,
                func.count().label("fail_count"),
            )
            .where(Task.status == "failed", Task.queued_at >= seven_days_ago, task_scope)
            .group_by(Task.agent_id)
            .order_by(func.count().desc())
            .limit(5)
        )
    ).all()

    fail_agent_ids = {r[0] for r in top_fail_rows}
    fail_agent_names: dict = {}
    if fail_agent_ids:
        name_rows = await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(fail_agent_ids)))
        fail_agent_names = dict(name_rows.all())

    top_failing = [
        {"agent_id": r[0], "agent_name": fail_agent_names.get(r[0], "Unknown"), "fail_count": r[1]}
        for r in top_fail_rows
    ]

    return {
        "time_series": time_series,
        "completion_metrics": {
            "avg_seconds": round(avg_seconds, 1),
            "p50_seconds": round(float(p50_row or 0), 1),
            "p95_seconds": round(float(p95_row or 0), 1),
        },
        "totals": {
            "total": total_in_period,
            "failed": failed_in_period,
            "success_rate": round(success_rate, 1),
        },
        "top_failing_agents": top_failing,
        "period_days": days,
    }
