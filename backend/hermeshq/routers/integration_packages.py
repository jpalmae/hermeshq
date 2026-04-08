from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.user import User
from hermeshq.schemas.managed_integration import ManagedIntegrationRead
from hermeshq.services.managed_capabilities import (
    install_uploaded_integration_package,
    list_available_integration_packages,
    uninstall_uploaded_integration_package,
)

router = APIRouter(prefix="/integration-packages", tags=["integration-packages"])


async def _load_enabled_slugs(db: AsyncSession) -> list[str]:
    settings = await db.get(AppSettings, "default")
    values = getattr(settings, "enabled_integration_packages", []) if settings else []
    return [slug for slug in values if isinstance(slug, str) and slug.strip()]


def _find_package(slug: str, enabled_slugs: list[str]) -> dict | None:
    for item in list_available_integration_packages(enabled_slugs):
        if item["slug"] == slug:
            return item
    return None


async def _sync_agents_for_package(
    request: Request,
    db: AsyncSession,
    package: dict,
    *,
    installing: bool,
) -> None:
    plugin_slug = package.get("plugin_slug")
    skill_identifier = package.get("skill_identifier")
    result = await db.execute(select(Agent))
    agents = list(result.scalars().all())
    for agent in agents:
        changed = False
        integration_enabled = package["slug"] in (agent.integration_configs or {})
        if plugin_slug:
            enabled_toolsets = list(agent.enabled_toolsets or [])
            if installing and integration_enabled and plugin_slug not in enabled_toolsets:
                enabled_toolsets.append(plugin_slug)
                agent.enabled_toolsets = enabled_toolsets
                changed = True
            if not installing and plugin_slug in enabled_toolsets:
                agent.enabled_toolsets = [toolset for toolset in enabled_toolsets if toolset != plugin_slug]
                changed = True
            if not installing and plugin_slug in (agent.disabled_toolsets or []):
                agent.disabled_toolsets = [toolset for toolset in (agent.disabled_toolsets or []) if toolset != plugin_slug]
                changed = True
        if not installing:
            configs = dict(agent.integration_configs or {})
            if package["slug"] in configs:
                configs.pop(package["slug"], None)
                agent.integration_configs = configs
                changed = True
            if skill_identifier and skill_identifier in (agent.skills or []):
                agent.skills = [skill for skill in (agent.skills or []) if skill != skill_identifier]
                changed = True
        if changed:
            await request.app.state.installation_manager.sync_agent_installation(agent)


@router.get("", response_model=list[ManagedIntegrationRead])
async def list_integration_packages(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> list[ManagedIntegrationRead]:
    enabled_slugs = await _load_enabled_slugs(db)
    return [ManagedIntegrationRead.model_validate(item) for item in list_available_integration_packages(enabled_slugs)]


@router.post("/upload", response_model=ManagedIntegrationRead, status_code=status.HTTP_201_CREATED)
async def upload_integration_package(
    request: Request,
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> ManagedIntegrationRead:
    suffix = "".join(Path(file.filename or "package.tar.gz").suffixes) or ".tar.gz"
    with NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        package = install_uploaded_integration_package(tmp_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    settings = await db.get(AppSettings, "default")
    if not settings:
        settings = AppSettings(id="default")
        db.add(settings)
    enabled = [slug for slug in (settings.enabled_integration_packages or []) if isinstance(slug, str)]
    if package["slug"] not in enabled:
        enabled.append(package["slug"])
    settings.enabled_integration_packages = enabled
    await _sync_agents_for_package(request, db, package, installing=True)
    await db.commit()
    return ManagedIntegrationRead.model_validate({**package, "installed": True})


@router.post("/{slug}/install", response_model=ManagedIntegrationRead)
async def install_integration_package(
    slug: str,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> ManagedIntegrationRead:
    enabled = await _load_enabled_slugs(db)
    package = _find_package(slug, enabled)
    if not package:
        raise HTTPException(status_code=404, detail="Integration package not found")
    settings = await db.get(AppSettings, "default")
    if not settings:
        settings = AppSettings(id="default")
        db.add(settings)
    next_enabled = [value for value in (settings.enabled_integration_packages or []) if isinstance(value, str)]
    if slug not in next_enabled:
        next_enabled.append(slug)
    settings.enabled_integration_packages = next_enabled
    await _sync_agents_for_package(request, db, package, installing=True)
    await db.commit()
    package["installed"] = True
    return ManagedIntegrationRead.model_validate(package)


@router.post("/{slug}/uninstall", status_code=status.HTTP_204_NO_CONTENT)
async def uninstall_integration_package(
    slug: str,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    enabled = await _load_enabled_slugs(db)
    package = _find_package(slug, enabled)
    if not package:
        raise HTTPException(status_code=404, detail="Integration package not found")

    settings = await db.get(AppSettings, "default")
    if settings:
        settings.enabled_integration_packages = [value for value in (settings.enabled_integration_packages or []) if value != slug]

    await _sync_agents_for_package(request, db, package, installing=False)

    if package.get("source_type") == "uploaded":
        uninstall_uploaded_integration_package(slug)

    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
