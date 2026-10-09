"""Agent runtime endpoints – start, stop, restart, mode changes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hermeshq.core.security import ensure_agent_access, get_current_user, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.node import Node
from hermeshq.models.user import User
from hermeshq.routers.agents_shared import _serialize_agent
from hermeshq.schemas.agent import AgentModeUpdate, AgentRead
from hermeshq.schemas.permission_policy import PermissionTestRequest, PermissionTestResult
from hermeshq.services.audit import extract_ip, record_audit

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


@router.post("/{agent_id}/service-token/rotate")
async def rotate_agent_service_token(
    agent_id: str,
    request: Request,
    admin_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    agent = await db.get(Agent, agent_id)
    if not agent or agent.is_archived:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.service_token_version = (agent.service_token_version or 1) + 1
    await record_audit(
        db,
        action="agent.service_token.rotate",
        target_type="agent",
        target_id=agent.id,
        target_name=agent.name,
        actor_id=admin_user.id,
        actor_username=admin_user.username,
        actor_role=admin_user.role,
        ip_address=extract_ip(request),
        details={"token_version": agent.service_token_version},
    )
    await db.commit()

    restarted: list[str] = []
    failures: dict[str, str] = {}
    if agent.status == "running":
        try:
            await request.app.state.supervisor.restart_agent(agent_id)
            restarted.append("agent")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to restart agent %s after service-token rotation", agent_id)
            failures["agent"] = str(exc)

    platforms = list(
        (
            await db.execute(
                select(MessagingChannel.platform).where(
                    MessagingChannel.agent_id == agent_id,
                    MessagingChannel.enabled.is_(True),
                )
            )
        ).scalars()
    )
    for platform in platforms:
        try:
            await request.app.state.gateway_supervisor.restart_channel(agent_id, platform)
            restarted.append(f"channel:{platform}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to restart %s channel for agent %s after token rotation", platform, agent_id)
            failures[f"channel:{platform}"] = str(exc)

    return {
        "agent_id": agent_id,
        "token_version": agent.service_token_version,
        "restarted": restarted,
        "failures": failures,
    }


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


@router.post("/{agent_id}/test-permission", response_model=PermissionTestResult)
async def test_permission(
    agent_id: str,
    payload: PermissionTestRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PermissionTestResult:
    """Dry-run: check if a tool/command would be allowed by the agent's permission policy."""
    agent = await ensure_agent_access(db, current_user, agent_id)
    if not agent.permission_policy_id:
        return PermissionTestResult(allowed=True)

    decision = await request.app.state.permission_enforcer.evaluate(agent, payload.tool, payload.input)
    return PermissionTestResult(
        allowed=decision.allowed,
        reason=decision.reason,
        policy_name=decision.policy_name,
        requires_approval=decision.requires_approval,
    )
