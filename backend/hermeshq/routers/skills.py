import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_current_user, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.user import User
from hermeshq.schemas.skill import AgentSkillsRead, SkillCatalogRead
from hermeshq.services.hermes_installation import HermesInstallationError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("/catalog", response_model=SkillCatalogRead)
async def get_skill_catalog(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
    _: User = Depends(get_current_user),
) -> dict:
    installation_manager = request.app.state.installation_manager
    skills = await installation_manager.search_catalog(q, limit=limit)
    return {"skills": skills, "count": len(skills), "query": q}


@router.get("/agents/{agent_id}", response_model=AgentSkillsRead)
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


@router.delete("/agents/{agent_id}/installed", response_model=AgentSkillsRead)
async def delete_agent_installed_skill(
    agent_id: str,
    request: Request,
    path: str = Query(..., min_length=1),
    current_user: User = Depends(require_admin),
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


@router.post("/agents/{agent_id}/adopt", response_model=AgentSkillsRead)
async def adopt_agent_shared_skill(
    agent_id: str,
    request: Request,
    name: str = Query(..., min_length=1),
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    agent = await ensure_agent_access(db, current_user, agent_id)
    installation_manager = request.app.state.installation_manager
    try:
        installed = await installation_manager.adoptar_skill_compartida(agent, name)
    except HermesInstallationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "agent_id": agent.id,
        "assigned": agent.skills,
        "installed": installed,
        "count": len(installed),
    }
