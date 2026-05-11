"""Agent runtime endpoints – start, stop, restart, mode changes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hermeshq.core.security import ensure_agent_access, get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.user import User
from hermeshq.schemas.agent import AgentModeUpdate, AgentRead

from hermeshq.routers.agents_shared import _serialize_agent

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/{agent_id}/start", response_model=AgentRead)
async def start_agent(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    await ensure_agent_access(db, current_user, agent_id)
    supervisor = request.app.state.supervisor
    try:
        await supervisor.start_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await db.execute(
        select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id)
    )
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
    result = await db.execute(
        select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id)
    )
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
    result = await db.execute(
        select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id)
    )
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
    result = await db.execute(
        select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id)
    )
    return _serialize_agent(request, result.scalar_one())
