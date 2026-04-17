from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.message import AgentMessage
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.schemas.runtime_ledger import RuntimeLedgerEntryRead, RuntimeLedgerResponse

router = APIRouter(prefix="/runtime-ledger", tags=["runtime-ledger"])


def _agent_label(agent: Agent | None) -> str | None:
    if not agent:
        return None
    return agent.friendly_name or agent.name or agent.slug or agent.id


def _task_channel(task: Task) -> str:
    metadata = task.metadata_json or {}
    if metadata.get("conversation") or metadata.get("source") == "agent_conversation":
        return "talk_to_agent"
    if metadata.get("scheduled") or metadata.get("scheduled_task_id"):
        return "schedule"
    if metadata.get("delegated") or task.source_agent_id:
        return "agent_to_agent"
    return "task"


def _task_counterpart(task: Task, agent_map: dict[str, Agent], viewer_agent_id: str) -> tuple[str | None, str | None]:
    if task.source_agent_id and task.source_agent_id != viewer_agent_id:
        counterpart = agent_map.get(task.source_agent_id)
        return task.source_agent_id, _agent_label(counterpart)
    return None, None


def _task_request_entry(task: Task, agent_map: dict[str, Agent]) -> RuntimeLedgerEntryRead:
    counterpart_id, counterpart_label = _task_counterpart(task, agent_map, task.agent_id)
    channel = _task_channel(task)
    metadata = task.metadata_json or {}
    title = task.title or {
        "talk_to_agent": "Talk to agent",
        "schedule": "Scheduled task",
        "agent_to_agent": "Delegated task",
    }.get(channel, "Task")
    return RuntimeLedgerEntryRead(
        id=f"task-request:{task.id}",
        agent_id=task.agent_id,
        channel=channel,
        direction="inbound",
        entry_type="request",
        title=title,
        content=task.prompt,
        status=task.status,
        task_id=task.id,
        message_id=None,
        counterpart_agent_id=counterpart_id,
        counterpart_label=counterpart_label,
        details=metadata,
        created_at=task.queued_at,
    )


def _task_response_entry(task: Task, agent_map: dict[str, Agent]) -> RuntimeLedgerEntryRead | None:
    content = (task.response or "").strip() or (task.error_message or "").strip()
    if not content and task.status not in {"completed", "failed", "cancelled"}:
        return None
    counterpart_id, counterpart_label = _task_counterpart(task, agent_map, task.agent_id)
    metadata = task.metadata_json or {}
    channel = _task_channel(task)
    created_at = task.completed_at or task.started_at or task.queued_at
    title = {
        "completed": "Task response",
        "failed": "Task error",
        "cancelled": "Task cancelled",
    }.get(task.status, "Task update")
    return RuntimeLedgerEntryRead(
        id=f"task-response:{task.id}",
        agent_id=task.agent_id,
        channel=channel,
        direction="outbound",
        entry_type="response",
        title=title,
        content=content or None,
        status=task.status,
        task_id=task.id,
        message_id=None,
        counterpart_agent_id=counterpart_id,
        counterpart_label=counterpart_label,
        details=metadata,
        created_at=created_at,
    )


def _message_entry(message: AgentMessage, viewer_agent_id: str, agent_map: dict[str, Agent]) -> RuntimeLedgerEntryRead:
    outbound = message.from_agent_id == viewer_agent_id
    counterpart_id = message.to_agent_id if outbound else message.from_agent_id
    counterpart = agent_map.get(counterpart_id) if counterpart_id else None
    return RuntimeLedgerEntryRead(
        id=f"message:{message.id}:{'out' if outbound else 'in'}",
        agent_id=viewer_agent_id,
        channel="agent_to_agent",
        direction="outbound" if outbound else "inbound",
        entry_type=message.message_type,
        title=message.message_type.replace("_", " ").title(),
        content=message.content,
        status=message.status,
        task_id=message.task_id,
        message_id=message.id,
        counterpart_agent_id=counterpart_id,
        counterpart_label=_agent_label(counterpart),
        details=message.metadata_json or {},
        created_at=message.created_at,
    )


def _runtime_activity_channel(event_type: str) -> str | None:
    if event_type.startswith("hq_control."):
        return "control_plane"
    if event_type in {"agent.started", "agent.stopped", "agent.archived", "agent.system_operator.created"}:
        return "runtime"
    if event_type == "schedule.triggered":
        return "schedule"
    if event_type.startswith("security."):
        return "integration"
    if event_type == "comms.delegate_result":
        return "agent_to_agent"
    if event_type.startswith("channel.") and event_type.endswith((".started", ".stopped", ".exited")):
        return "runtime"
    return None


def _activity_counterpart(
    log: ActivityLog,
    viewer_agent_id: str,
    agent_map: dict[str, Agent],
) -> tuple[str | None, str | None]:
    details = log.details or {}
    system_agent_id = str(details.get("system_agent_id") or "").strip() or None
    if log.event_type.startswith("hq_control."):
        if viewer_agent_id == system_agent_id:
            target_id = log.agent_id if log.agent_id != viewer_agent_id else None
            if target_id:
                return target_id, _agent_label(agent_map.get(target_id))
            return None, None
        if system_agent_id and system_agent_id != viewer_agent_id:
            return system_agent_id, _agent_label(agent_map.get(system_agent_id))
    if log.event_type == "comms.delegate_result":
        delegated_agent_id = str(details.get("delegated_agent_id") or "").strip() or None
        if delegated_agent_id and delegated_agent_id != viewer_agent_id:
            return delegated_agent_id, _agent_label(agent_map.get(delegated_agent_id))
    return None, None


def _activity_entry(
    log: ActivityLog,
    viewer_agent_id: str,
    agent_map: dict[str, Agent],
) -> RuntimeLedgerEntryRead | None:
    channel = _runtime_activity_channel(log.event_type)
    if not channel:
        return None
    counterpart_id, counterpart_label = _activity_counterpart(log, viewer_agent_id, agent_map)
    return RuntimeLedgerEntryRead(
        id=f"log:{log.id}",
        agent_id=viewer_agent_id,
        channel=channel,
        direction="system",
        entry_type=log.event_type,
        title=log.message or log.event_type.replace("_", " ").replace(".", " ").title(),
        content=None,
        status=log.severity,
        task_id=log.task_id,
        message_id=None,
        counterpart_agent_id=counterpart_id,
        counterpart_label=counterpart_label,
        details=log.details or {},
        created_at=log.created_at,
    )


@router.get("", response_model=RuntimeLedgerResponse)
async def list_runtime_ledger(
    agent_id: str = Query(...),
    limit: int = Query(default=200, le=1000),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> RuntimeLedgerResponse:
    viewer_agent = await ensure_agent_access(db, current_user, agent_id)

    task_result = await db.execute(
        select(Task)
        .where(Task.agent_id == agent_id)
        .order_by(desc(Task.queued_at))
        .limit(limit)
    )
    tasks = task_result.scalars().all()

    message_result = await db.execute(
        select(AgentMessage)
        .where(or_(AgentMessage.from_agent_id == agent_id, AgentMessage.to_agent_id == agent_id))
        .order_by(desc(AgentMessage.created_at))
        .limit(limit)
    )
    messages = message_result.scalars().all()

    activity_result = await db.execute(
        select(ActivityLog)
        .where(
            ActivityLog.agent_id == agent_id,
            or_(
                ActivityLog.event_type.like("hq_control.%"),
                ActivityLog.event_type.like("agent.%"),
                ActivityLog.event_type.like("security.%"),
                ActivityLog.event_type.like("channel.%.started"),
                ActivityLog.event_type.like("channel.%.stopped"),
                ActivityLog.event_type.like("channel.%.exited"),
                ActivityLog.event_type == "schedule.triggered",
                ActivityLog.event_type == "comms.delegate_result",
            ),
        )
        .order_by(desc(ActivityLog.created_at))
        .limit(limit * 4)
    )
    activity_logs = activity_result.scalars().all()

    if viewer_agent.is_system_agent:
        actor_activity_result = await db.execute(
            select(ActivityLog)
            .where(ActivityLog.event_type.like("hq_control.%"))
            .order_by(desc(ActivityLog.created_at))
            .limit(limit * 6)
        )
        for log in actor_activity_result.scalars().all():
            details = log.details or {}
            if str(details.get("system_agent_id") or "").strip() == agent_id:
                activity_logs.append(log)

    deduped_logs: dict[str, ActivityLog] = {log.id: log for log in activity_logs}
    activity_logs = list(deduped_logs.values())

    related_agent_ids = {
        related_id
        for related_id in (
            *(task.source_agent_id for task in tasks),
            *(message.from_agent_id for message in messages),
            *(message.to_agent_id for message in messages),
            *(log.agent_id for log in activity_logs),
            *(
                str((log.details or {}).get("system_agent_id") or "").strip() or None
                for log in activity_logs
            ),
            *(
                str((log.details or {}).get("delegated_agent_id") or "").strip() or None
                for log in activity_logs
            ),
        )
        if related_id
    }
    agent_map: dict[str, Agent] = {}
    if related_agent_ids:
        agent_rows = await db.execute(select(Agent).where(Agent.id.in_(related_agent_ids)))
        agent_map = {agent.id: agent for agent in agent_rows.scalars().all()}

    entries: list[RuntimeLedgerEntryRead] = []
    for task in tasks:
        entries.append(_task_request_entry(task, agent_map))
        response_entry = _task_response_entry(task, agent_map)
        if response_entry:
            entries.append(response_entry)
    for message in messages:
        entries.append(_message_entry(message, agent_id, agent_map))
    for log in activity_logs:
        entry = _activity_entry(log, agent_id, agent_map)
        if entry:
            entries.append(entry)

    entries.sort(key=lambda item: item.created_at, reverse=True)
    return RuntimeLedgerResponse(entries=entries[:limit])
