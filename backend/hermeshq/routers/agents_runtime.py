"""Agent runtime endpoints – start, stop, restart, mode changes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hermeshq.core.security import ensure_agent_access, get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.models.user import User
from hermeshq.routers.agents_shared import _serialize_agent
from hermeshq.schemas.agent import AgentModeUpdate, AgentRead

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/{agent_id}/start", response_model=AgentRead)
async def start_agent(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    agent = await ensure_agent_access(db, current_user, agent_id)
    node = await db.get(Node, agent.node_id)
    if node:
        active_count_result = await db.execute(
            select(func.count()).where(
                Agent.node_id == agent.node_id,
                Agent.status.in_(("running", "starting")),
                Agent.is_archived.is_(False),
            )
        )
        active_count = active_count_result.scalar_one()
        if active_count >= node.max_agents:
            raise HTTPException(
                status_code=409,
                detail=f"Node is at capacity ({active_count}/{node.max_agents} agents running)",
            )
    supervisor = request.app.state.supervisor
    try:
        await supervisor.start_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    return _serialize_agent(request, result.scalar_one())


@router.post("/{agent_id}/stop", response_model=AgentRead)
async def stop_agent(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    await ensure_agent_access(db, current_user, agent_id)
    supervisor = request.app.state.supervisor
    await supervisor.stop_agent(agent_id)
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    return _serialize_agent(request, result.scalar_one())


@router.post("/{agent_id}/restart", response_model=AgentRead)
async def restart_agent(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    await ensure_agent_access(db, current_user, agent_id)
    supervisor = request.app.state.supervisor
    try:
        await supervisor.restart_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    return _serialize_agent(request, result.scalar_one())


@router.post("/{agent_id}/mode", response_model=AgentRead)
async def set_agent_mode(
    agent_id: str,
    payload: AgentModeUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    agent = await ensure_agent_access(db, current_user, agent_id)
    mode = payload.mode
    if mode not in {"headless", "interactive", "hybrid"}:
        raise HTTPException(status_code=400, detail="Invalid mode")
    agent.run_mode = mode
    await db.commit()
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    return _serialize_agent(request, result.scalar_one())


@router.post("/{agent_id}/test-permission")
async def test_permission(
    agent_id: str,
    payload: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Dry-run: check if a tool/command would be allowed by the agent's permission policy."""
    agent = await ensure_agent_access(db, current_user, agent_id)
    if not agent.permission_policy_id:
        return {"allowed": True, "reason": None, "policy_name": None, "requires_approval": False}

    from hermeshq.models.permission_policy import PermissionPolicy
    from hermeshq.services.permission_enforcer import PermissionEnforcer
    from hermeshq.database import AsyncSessionLocal

    policy = await db.get(PermissionPolicy, agent.permission_policy_id)
    enforcer = PermissionEnforcer(AsyncSessionLocal)
    tool_name = payload.get("tool", "")
    tool_input = payload.get("input", {})
    allowed, reason, requires_approval = await enforcer.evaluate(agent, tool_name, tool_input)
    return {
        "allowed": allowed,
        "reason": reason,
        "policy_name": policy.name if policy else None,
        "requires_approval": requires_approval,
    }
