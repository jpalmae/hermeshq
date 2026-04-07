from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.user import User
from hermeshq.services.hermes_installation import HermesInstallationError

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("/catalog")
async def get_skill_catalog(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    _: User = Depends(get_current_user),
) -> dict:
    installation_manager = request.app.state.installation_manager
    skills = await installation_manager.search_catalog(q, limit=limit)
    return {"skills": skills, "count": len(skills), "query": q}


@router.get("/agents/{agent_id}")
async def get_agent_skills(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    agent = await ensure_agent_access(db, current_user, agent_id)
    installation_manager = request.app.state.installation_manager
    installed = await installation_manager.list_installed_skills(agent)
    return {
        "agent_id": agent.id,
        "assigned": agent.skills,
        "installed": installed,
        "count": len(installed),
    }


@router.delete("/agents/{agent_id}/installed")
async def delete_agent_installed_skill(
    agent_id: str,
    request: Request,
    path: str = Query(..., min_length=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    agent = await ensure_agent_access(db, current_user, agent_id)
    installation_manager = request.app.state.installation_manager
    try:
        installed = await installation_manager.delete_installed_skill(agent, path)
    except HermesInstallationError as exc:
        raise HTTPException(status_code=400 if "Invalid" in str(exc) else 404, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(agent)
    return {
        "agent_id": agent.id,
        "assigned": agent.skills,
        "installed": installed,
        "count": len(installed),
    }
