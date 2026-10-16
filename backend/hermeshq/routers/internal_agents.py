import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import can_access_agent, verify_agent_service_token
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.routers.agents_shared import _load_agent_map
from hermeshq.schemas.internal_agent import InternalDelegateRead, InternalDirectRead, InternalRosterRead
from hermeshq.schemas.message import MessageCreate
from hermeshq.services.agent_hierarchy import delegate_route, route_label, validate_delegate_hierarchy
from hermeshq.services.cron_schedule import (
    APPROVAL_CODE_TTL,
    DEFAULT_TIMEZONE,
    MAX_AGENT_NAME_CHARS,
    MAX_AGENT_PROMPT_CHARS,
    MAX_AGENT_SCHEDULES,
    CronScheduleError,
    compute_next_run,
    generate_approval_code,
    message_contains_code,
    resolve_timezone,
    validate_agent_expression,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/agents/self", tags=["internal-agents"], include_in_schema=False)


class InternalDirectRequest(BaseModel):
    target_agent: str
    content: str
    metadata: dict[str, Any] = {}


class InternalDelegateRequest(BaseModel):
    target_agent: str
    instruction: str
    title: str | None = None
    metadata: dict[str, Any] = {}


async def _get_internal_agent(
    db: AsyncSession = Depends(get_db_session),
    agent_id: str | None = Header(default=None, alias="X-HermesHQ-Agent-ID"),
    agent_token: str | None = Header(default=None, alias="X-HermesHQ-Agent-Token"),
) -> Agent:
    if not agent_id or not agent_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing agent credentials")
    agent = await db.get(Agent, agent_id)
    if not agent or agent.is_archived:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown agent")
    if not verify_agent_service_token(agent_token, agent_id, agent.service_token_version or 1):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent credentials")
    return agent


def _display_name(agent: Agent) -> str:
    return agent.friendly_name or agent.name or agent.slug or agent.id


def _match_score(agent: Agent, target: str) -> int:
    lowered = target.strip().lower()
    if not lowered:
        return -1
    if agent.id == target:
        return 100
    if (agent.slug or "").lower() == lowered:
        return 90
    if (agent.friendly_name or "").lower() == lowered:
        return 80
    if (agent.name or "").lower() == lowered:
        return 70
    return -1


def _resolve_target_agent(agent_map: dict[str, Agent], target: str, source_agent_id: str) -> Agent:
    exact_matches = [
        agent for agent in agent_map.values() if agent.id != source_agent_id and _match_score(agent, target) >= 0
    ]
    if not exact_matches:
        raise HTTPException(status_code=404, detail=f"No agent matched '{target}'")
    exact_matches.sort(key=lambda item: _match_score(item, target), reverse=True)
    best_score = _match_score(exact_matches[0], target)
    best_matches = [item for item in exact_matches if _match_score(item, target) == best_score]
    if len(best_matches) > 1:
        options = ", ".join(_display_name(item) for item in best_matches[:6])
        raise HTTPException(status_code=409, detail=f"Ambiguous agent target '{target}'. Matches: {options}")
    return best_matches[0]


@router.get("/roster", response_model=InternalRosterRead)
async def roster(
    current_agent: Agent = Depends(_get_internal_agent),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    agent_map = await _load_agent_map(db)
    self_agent = agent_map[current_agent.id]
    items: list[dict[str, Any]] = []
    for agent in agent_map.values():
        allowed, route = delegate_route(agent_map, self_agent, agent)
        items.append(
            {
                "id": agent.id,
                "display_name": _display_name(agent),
                "slug": agent.slug,
                "description": (agent.description or "").strip(),
                "status": agent.status,
                "self": agent.id == self_agent.id,
                "can_send_tasks": bool(agent.can_send_tasks),
                "can_receive_tasks": bool(agent.can_receive_tasks),
                "supervisor_agent_id": agent.supervisor_agent_id,
                "supervisor": _display_name(agent_map[agent.supervisor_agent_id])
                if agent.supervisor_agent_id and agent.supervisor_agent_id in agent_map
                else None,
                "team_tags": list(agent.team_tags or []),
                "delegate_allowed": bool(allowed),
                "delegate_route": route,
                "delegate_reason": route_label(route),
            }
        )
    return {
        "self": {
            "id": self_agent.id,
            "display_name": _display_name(self_agent),
            "slug": self_agent.slug,
        },
        "agents": items,
    }


@router.post("/direct", response_model=InternalDirectRead)
async def direct_message(
    payload: InternalDirectRequest,
    request: Request,
    current_agent: Agent = Depends(_get_internal_agent),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    agent_map = await _load_agent_map(db)
    target_agent = _resolve_target_agent(agent_map, payload.target_agent, current_agent.id)
    message = await request.app.state.comms_router.send_message(
        MessageCreate(
            from_agent_id=current_agent.id,
            to_agent_id=target_agent.id,
            message_type="direct",
            content=payload.content,
            metadata=payload.metadata,
        )
    )
    return {
        "success": True,
        "message_id": message.id,
        "task_id": message.task_id,
        "from_agent": _display_name(current_agent),
        "to_agent": _display_name(target_agent),
        "message_type": "direct",
    }


@router.post("/delegate", response_model=InternalDelegateRead)
async def delegate_task(
    payload: InternalDelegateRequest,
    request: Request,
    current_agent: Agent = Depends(_get_internal_agent),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    agent_map = await _load_agent_map(db)
    target_agent = _resolve_target_agent(agent_map, payload.target_agent, current_agent.id)
    validate_delegate_hierarchy(agent_map, current_agent, target_agent)
    metadata = payload.metadata.copy()
    if payload.title:
        metadata["title"] = payload.title
    raw_policy_master = metadata.get("policy_master_agent_id")
    if not (isinstance(raw_policy_master, str) and raw_policy_master.strip()):
        metadata["policy_master_agent_id"] = current_agent.id
    raw_parent_task_id = metadata.get("parent_task_id")
    if isinstance(raw_parent_task_id, str) and raw_parent_task_id.strip():
        parent_task = await db.get(Task, raw_parent_task_id.strip())
        if not parent_task or parent_task.agent_id != current_agent.id:
            metadata.pop("parent_task_id", None)
    message = await request.app.state.comms_router.send_message(
        MessageCreate(
            from_agent_id=current_agent.id,
            to_agent_id=target_agent.id,
            message_type="delegate",
            content=payload.instruction,
            metadata=metadata,
        )
    )
    if message.task_id:
        should_start = target_agent.status != "running"
        if should_start:
            await request.app.state.supervisor.start_agent(target_agent.id)
        await request.app.state.supervisor.submit_task(message.task_id)
    allowed, route = delegate_route(agent_map, current_agent, target_agent)
    return {
        "success": True,
        "message_id": message.id,
        "task_id": message.task_id,
        "from_agent": _display_name(current_agent),
        "to_agent": _display_name(target_agent),
        "message_type": "delegate",
        "delegate_allowed": bool(allowed),
        "delegate_route": route,
        "delegate_reason": route_label(route),
    }


class InternalScheduleCreate(BaseModel):
    name: str
    cron_expression: str
    prompt: str
    requested_for_user_id: str | None = None
    task_id: str | None = None


def _local_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(resolve_timezone(DEFAULT_TIMEZONE)).strftime("%Y-%m-%d %H:%M %Z")


def _schedule_view(item: ScheduledTask) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "cron_expression": item.cron_expression,
        "timezone": item.timezone,
        "prompt": item.prompt,
        "approval_status": item.approval_status,
        "enabled": item.enabled,
        "created_by_agent": item.created_by_agent_id is not None,
        "next_run_local": _local_time(item.next_run),
        "last_run_local": _local_time(item.last_run),
    }


@router.get("/schedules")
async def list_own_schedules(
    agent: Agent = Depends(_get_internal_agent),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    result = await db.execute(
        select(ScheduledTask).where(ScheduledTask.agent_id == agent.id).order_by(ScheduledTask.created_at.asc())
    )
    return {"success": True, "schedules": [_schedule_view(item) for item in result.scalars().all()]}


@router.post("/schedules", status_code=status.HTTP_201_CREATED)
async def create_own_schedule(
    payload: InternalScheduleCreate,
    agent: Agent = Depends(_get_internal_agent),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    user_id = (payload.requested_for_user_id or "").strip()
    if not user_id:
        raise HTTPException(
            status_code=400,
            detail="Schedules can only be requested from a conversation with an identified user",
        )
    user = await db.get(User, user_id)
    if not user or not user.is_active or not await can_access_agent(db, user, agent.id):
        raise HTTPException(status_code=403, detail="The requesting user cannot schedule tasks for this agent")

    name = payload.name.strip()
    prompt = payload.prompt.strip()
    cron_expression = " ".join(payload.cron_expression.split())
    if not name or not prompt:
        raise HTTPException(status_code=400, detail="'name' and 'prompt' are required")
    if len(name) > MAX_AGENT_NAME_CHARS:
        raise HTTPException(status_code=400, detail=f"'name' must be at most {MAX_AGENT_NAME_CHARS} characters")
    if len(prompt) > MAX_AGENT_PROMPT_CHARS:
        raise HTTPException(status_code=400, detail=f"'prompt' must be at most {MAX_AGENT_PROMPT_CHARS} characters")
    try:
        first_run = validate_agent_expression(cron_expression, DEFAULT_TIMEZONE)
    except CronScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.execute(select(Agent.id).where(Agent.id == agent.id).with_for_update())
    existing_result = await db.execute(select(ScheduledTask).where(ScheduledTask.agent_id == agent.id))
    existing = list(existing_result.scalars().all())
    own_count = sum(1 for item in existing if item.created_by_agent_id == agent.id)
    if own_count >= MAX_AGENT_SCHEDULES:
        raise HTTPException(
            status_code=409,
            detail=f"Limit of {MAX_AGENT_SCHEDULES} agent-created schedules reached; delete one first",
        )
    for item in existing:
        if item.name.strip().lower() == name.lower() or (
            item.cron_expression == cron_expression and item.prompt.strip() == prompt
        ):
            raise HTTPException(
                status_code=409,
                detail=f"A similar schedule already exists (id {item.id}, status {item.approval_status})",
            )

    origin_task = await db.get(Task, payload.task_id.strip()) if payload.task_id else None
    from_public_chat = bool(
        origin_task
        and origin_task.agent_id == agent.id
        and (origin_task.metadata_json or {}).get("source") == "public_chat"
    )
    item = ScheduledTask(
        agent_id=agent.id,
        name=name,
        cron_expression=cron_expression,
        prompt=prompt,
        enabled=False,
        timezone=DEFAULT_TIMEZONE,
        owner_user_id=user.id,
        approval_status="pending",
        approval_code=generate_approval_code(),
        created_by_agent_id=agent.id,
        notify_public_chat=from_public_chat,
        next_run=None,
    )
    db.add(item)
    await db.flush()
    db.add(
        ActivityLog(
            agent_id=agent.id,
            event_type="schedule.pending_approval",
            message=name,
            details={"schedule_id": item.id, "requested_for_user_id": user.id, "cron_expression": cron_expression},
        )
    )
    await db.commit()
    return {
        "success": True,
        "schedule_id": item.id,
        "approval_status": "pending",
        "approval_code": item.approval_code,
        "timezone": DEFAULT_TIMEZONE,
        "first_run_if_approved_now": _local_time(first_run),
        "message": (
            "Schedule created but NOT active. Show the user the name, the schedule in Chile time and the exact "
            f"prompt, then tell them to reply exactly 'aprobar {item.approval_code}' in this same chat. After "
            "that reply, call hq_schedule_approve. The user can also approve it in HermesHQ > Scheduled Tasks. "
            "Results of scheduled runs always appear in HermesHQ web (Tasks and the agent conversation panel). "
            "They are also queued for delivery to this chat when the chat bridge supports notifications, so do "
            "not promise it will arrive here."
        ),
    }


class InternalScheduleApprove(BaseModel):
    task_id: str | None = None


@router.post("/schedules/{schedule_id}/approve")
async def approve_own_schedule(
    schedule_id: str,
    payload: InternalScheduleApprove,
    agent: Agent = Depends(_get_internal_agent),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    task = await db.get(Task, (payload.task_id or "").strip()) if payload.task_id else None
    if not task or task.agent_id != agent.id or task.status != "running":
        raise HTTPException(status_code=403, detail="Approval must happen inside a live conversation with the user")
    task_metadata = task.metadata_json or {}
    if task_metadata.get("scheduled"):
        raise HTTPException(status_code=403, detail="Schedules cannot be approved from a scheduled run")
    user_id = str(task_metadata.get("thread_user_id") or "").strip()
    item = await db.get(ScheduledTask, schedule_id)
    if not item or item.agent_id != agent.id or item.created_by_agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if item.approval_status != "pending":
        raise HTTPException(status_code=409, detail="Schedule is not pending approval")
    if not user_id or item.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the user who requested this schedule can approve it")
    created_at = item.created_at if item.created_at.tzinfo else item.created_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - created_at > APPROVAL_CODE_TTL:
        raise HTTPException(status_code=410, detail="The approval code expired; request the schedule again")
    if not message_contains_code(task.prompt, item.approval_code):
        raise HTTPException(
            status_code=403,
            detail="The user's latest message does not contain the approval code. Ask them to reply 'aprobar CODE'",
        )
    item.approval_status = "approved"
    item.enabled = True
    item.approval_code = None
    item.next_run = compute_next_run(item.cron_expression, item.timezone)
    db.add(
        ActivityLog(
            agent_id=agent.id,
            task_id=task.id,
            event_type="schedule.approved",
            message=item.name,
            details={"schedule_id": item.id, "approved_by": user_id, "channel": task_metadata.get("source")},
        )
    )
    await db.commit()
    return {
        "success": True,
        "schedule_id": item.id,
        "approval_status": "approved",
        "enabled": True,
        "next_run_local": _local_time(item.next_run),
        "timezone": item.timezone,
    }


@router.delete("/schedules/{schedule_id}")
async def delete_own_schedule(
    schedule_id: str,
    agent: Agent = Depends(_get_internal_agent),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    item = await db.get(ScheduledTask, schedule_id)
    if not item or item.agent_id != agent.id:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if item.created_by_agent_id != agent.id:
        raise HTTPException(status_code=403, detail="Only schedules created by this agent can be deleted by it")
    await db.delete(item)
    db.add(
        ActivityLog(
            agent_id=agent.id,
            event_type="schedule.deleted_by_agent",
            message=item.name,
            details={"schedule_id": schedule_id},
        )
    )
    await db.commit()
    return {"success": True, "deleted": schedule_id}
