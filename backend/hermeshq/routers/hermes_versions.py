from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.user import User
from hermeshq.schemas.hermes_version import HermesVersionCreate, HermesVersionRead, HermesVersionUpdate
from hermeshq.services.hermes_version_manager import HermesVersionError

router = APIRouter(prefix="/hermes-versions", tags=["hermes-versions"])


@router.get("", response_model=list[HermesVersionRead])
async def list_hermes_versions(
    request: Request,
    _: User = Depends(require_admin),
) -> list[HermesVersionRead]:
    return await request.app.state.hermes_version_manager.list_versions()


@router.post("", response_model=HermesVersionRead, status_code=status.HTTP_201_CREATED)
async def create_hermes_version_catalog_entry(
    payload: HermesVersionCreate,
    request: Request,
    _: User = Depends(require_admin),
) -> HermesVersionRead:
    try:
        return await request.app.state.hermes_version_manager.create_catalog_entry(payload)
    except HermesVersionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{version}", response_model=HermesVersionRead)
async def update_hermes_version_catalog_entry(
    version: str,
    payload: HermesVersionUpdate,
    request: Request,
    _: User = Depends(require_admin),
) -> HermesVersionRead:
    try:
        return await request.app.state.hermes_version_manager.update_catalog_entry(version, payload)
    except HermesVersionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{version}/install", response_model=HermesVersionRead, status_code=status.HTTP_201_CREATED)
async def install_hermes_version(
    version: str,
    request: Request,
    _: User = Depends(require_admin),
) -> HermesVersionRead:
    try:
        installed = await request.app.state.hermes_version_manager.install_version(version)
    except HermesVersionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return installed


@router.delete("/{version}", status_code=status.HTTP_204_NO_CONTENT)
async def uninstall_hermes_version(
    version: str,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    version_manager = request.app.state.hermes_version_manager
    if version == "bundled":
        raise HTTPException(status_code=400, detail="Bundled Hermes runtime cannot be removed")
    default_version = await version_manager.get_default_version()
    if default_version == version:
        raise HTTPException(status_code=400, detail="Cannot uninstall the current default Hermes version")
    pinned_count = int(
        (
            await db.execute(select(func.count()).select_from(Agent).where(Agent.hermes_version == version))
        ).scalar_one()
        or 0
    )
    if pinned_count:
        raise HTTPException(status_code=400, detail="Cannot uninstall a Hermes version still pinned by agents")
    try:
        await version_manager.uninstall_version(version)
    except HermesVersionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{version}/catalog", status_code=status.HTTP_204_NO_CONTENT)
async def delete_hermes_version_catalog_entry(
    version: str,
    request: Request,
    _: User = Depends(require_admin),
) -> None:
    if version == "bundled":
        raise HTTPException(status_code=400, detail="Bundled Hermes runtime is not a catalog entry")
    try:
        await request.app.state.hermes_version_manager.delete_catalog_entry(version)
    except HermesVersionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
