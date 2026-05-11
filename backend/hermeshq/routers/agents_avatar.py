"""Agent avatar endpoints – get, upload, delete."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hermeshq.core.security import ensure_agent_access, get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.user import User
from hermeshq.schemas.agent import AgentRead
from hermeshq.services.avatar import (
    delete_avatar_files as _delete_avatar_files_shared,
    resolve_media_type,
    validate_and_save_avatar,
)

from hermeshq.routers.agents_shared import (
    _agent_avatar_base,
    _build_avatar_path,
    _serialize_agent,
)

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/{agent_id}/avatar", include_in_schema=False)
async def get_agent_avatar(agent_id: str, db: AsyncSession = Depends(get_db_session)):
    agent = await db.get(Agent, agent_id)
    if not agent or not agent.avatar_filename:
        raise HTTPException(status_code=404, detail="Avatar not found")
    avatar_path = _build_avatar_path(agent)
    if not avatar_path or not avatar_path.exists():
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(avatar_path, media_type=resolve_media_type(avatar_path))


@router.post("/{agent_id}/avatar", response_model=AgentRead)
async def upload_agent_avatar(
    agent_id: str,
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    agent = await ensure_agent_access(db, current_user, agent_id)
    agent.avatar_filename = await validate_and_save_avatar(_agent_avatar_base(), agent_id, file)
    await db.commit()
    await db.refresh(agent)
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    return _serialize_agent(request, result.scalar_one())


@router.delete("/{agent_id}/avatar", response_model=AgentRead)
async def delete_agent_avatar(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    agent = await ensure_agent_access(db, current_user, agent_id)
    _delete_avatar_files_shared(_agent_avatar_base(), agent_id)
    agent.avatar_filename = None
    await db.commit()
    await db.refresh(agent)
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    return _serialize_agent(request, result.scalar_one())
