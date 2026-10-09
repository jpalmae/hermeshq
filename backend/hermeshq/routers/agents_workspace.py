"""Agent workspace endpoints – browse files, read/write file content."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.user import User
from hermeshq.schemas.agent import (
    WorkspaceFileRead,
    WorkspaceFileWrite,
    WorkspaceFileWriteResult,
    WorkspaceListingRead,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/{agent_id}/workspace", response_model=WorkspaceListingRead)
async def list_workspace(
    agent_id: str,
    request: Request,
    path: str = Query(default="."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await ensure_agent_access(db, current_user, agent_id)
    try:
        return {
            "entries": request.app.state.workspace_manager.list_workspace_files(agent_id, path),
            "size": request.app.state.workspace_manager.get_workspace_size(agent_id),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid workspace path") from exc


@router.get("/{agent_id}/workspace/{file_path:path}", response_model=WorkspaceFileRead)
async def read_workspace_file(
    agent_id: str,
    file_path: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await ensure_agent_access(db, current_user, agent_id)
    try:
        content = request.app.state.workspace_manager.read_workspace_file(agent_id, file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Workspace file not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid workspace path") from exc
    return {"path": file_path, "content": content}


@router.put("/{agent_id}/workspace/{file_path:path}", response_model=WorkspaceFileWriteResult)
async def write_workspace_file(
    agent_id: str,
    file_path: str,
    payload: WorkspaceFileWrite,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await ensure_agent_access(db, current_user, agent_id)
    try:
        request.app.state.workspace_manager.write_workspace_file(agent_id, file_path, payload.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid workspace path") from exc
    return {"status": "ok", "path": file_path}
