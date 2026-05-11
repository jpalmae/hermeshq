"""Agent workspace endpoints – browse files, read/write file content."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.user import User
from hermeshq.schemas.agent import WorkspaceFileWrite

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/{agent_id}/workspace")
async def list_workspace(
    agent_id: str,
    request: Request,
    path: str = Query(default="."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await ensure_agent_access(db, current_user, agent_id)
    return {
        "entries": request.app.state.workspace_manager.list_workspace_files(agent_id, path),
        "size": request.app.state.workspace_manager.get_workspace_size(agent_id),
    }


@router.get("/{agent_id}/workspace/{file_path:path}")
async def read_workspace_file(
    agent_id: str,
    file_path: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await ensure_agent_access(db, current_user, agent_id)
    return {"path": file_path, "content": request.app.state.workspace_manager.read_workspace_file(agent_id, file_path)}


@router.put("/{agent_id}/workspace/{file_path:path}")
async def write_workspace_file(
    agent_id: str,
    file_path: str,
    payload: WorkspaceFileWrite,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await ensure_agent_access(db, current_user, agent_id)
    request.app.state.workspace_manager.write_workspace_file(agent_id, file_path, payload.content)
    return {"status": "ok", "path": file_path}
