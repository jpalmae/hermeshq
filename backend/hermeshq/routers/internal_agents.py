from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import create_agent_service_token
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.task import Task
from hermeshq.schemas.message import MessageCreate
from hermeshq.services.agent_hierarchy import delegate_route, route_label, validate_delegate_hierarchy

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
    expected = create_agent_service_token(agent_id)
    if agent_token != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent credentials")
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown agent")
    return agent


async def _load_agent_map(db: AsyncSession) -> dict[str, Agent]:
    result = await db.execute(select(Agent).order_by(Agent.created_at.asc()))
    return {agent.id: agent for agent in result.scalars().all()}


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
        agent
        for agent in agent_map.values()
        if agent.id != source_agent_id and _match_score(agent, target) >= 0
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


@router.get("/roster")
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
                "supervisor": _display_name(agent_map[agent.supervisor_agent_id]) if agent.supervisor_agent_id and agent.supervisor_agent_id in agent_map else None,
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


@router.post("/direct")
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


@router.post("/delegate")
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
