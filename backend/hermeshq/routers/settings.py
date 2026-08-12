import logging
import mimetypes
import re
from pathlib import Path

import aiofiles
import yaml
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.user import User
from hermeshq.schemas.settings import (
    AppSettingsRead,
    AppSettingsUpdate,
    GenerateOverrideRequest,
    GenerateOverrideResponse,
    PublicSettingsRead,
    ResourceStatusResponse,
    SemaphoreUpdateRequest,
    SemaphoreUpdateResponse,
)
from hermeshq.services.audit import extract_ip, record_audit, redact_sensitive
from hermeshq.services.resource_monitor import resource_monitor
from hermeshq.services.secret_vault import encrypt_value
from hermeshq.versioning import get_app_version

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])
settings = get_settings()
MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_FAVICON_BYTES = 512 * 1024
MAX_TUI_SKIN_BYTES = 256 * 1024
MANAGED_SKIN_PREFIX = "hermeshq-global-"
MASKED_RESEND_API_KEY = "••••••••"


async def _get_or_create_settings(db: AsyncSession) -> AppSettings:
    item = await db.get(AppSettings, "default")
    if item:
        return item
    item = AppSettings(id="default")
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


def _branding_path(filename: str) -> Path:
    return settings.branding_root / filename


def _settings_to_read(item: AppSettings) -> AppSettingsRead:
    version = int(item.updated_at.timestamp()) if item.updated_at else 0
    logo_url = f"/api/settings/branding/logo?v={version}" if item.logo_filename else None
    favicon_url = f"/api/settings/branding/favicon?v={version}" if item.favicon_filename else None
    return AppSettingsRead(
        id=item.id,
        app_version=get_app_version(),
        app_name=item.app_name or settings.app_name,
        app_short_name=item.app_short_name or (item.app_name or settings.app_name),
        theme_mode=item.theme_mode or "dark",
        default_locale=item.default_locale or "en",
        default_provider=item.default_provider,
        default_model=item.default_model,
        default_api_key_ref=item.default_api_key_ref,
        default_base_url=item.default_base_url,
        default_hermes_version=item.default_hermes_version,
        default_tui_skin=item.default_tui_skin,
        resend_api_key=MASKED_RESEND_API_KEY if item.resend_api_key else None,
        from_email=item.from_email,
        from_name=item.from_name,
        public_base_url=item.public_base_url,
        mfa_email_enabled=bool(item.mfa_email_enabled),
        tui_skin_filename=item.tui_skin_filename,
        logo_url=logo_url,
        favicon_url=favicon_url,
        has_tui_skin=bool(item.tui_skin_filename and item.default_tui_skin),
        has_logo=bool(item.logo_filename),
        has_favicon=bool(item.favicon_filename),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _settings_to_public_read(item: AppSettings) -> PublicSettingsRead:
    """Build a safe, unauthenticated subset of settings (no secrets/refs)."""
    version = int(item.updated_at.timestamp()) if item.updated_at else 0
    logo_url = f"/api/settings/branding/logo?v={version}" if item.logo_filename else None
    favicon_url = f"/api/settings/branding/favicon?v={version}" if item.favicon_filename else None
    return PublicSettingsRead(
        app_version=get_app_version(),
        app_name=item.app_name or settings.app_name,
        app_short_name=item.app_short_name or (item.app_name or settings.app_name),
        theme_mode=item.theme_mode or "dark",
        default_locale=item.default_locale or "en",
        logo_url=logo_url,
        favicon_url=favicon_url,
        has_logo=bool(item.logo_filename),
        has_favicon=bool(item.favicon_filename),
    )


def _validate_upload(kind: str, file: UploadFile, content: bytes) -> str:
    filename = file.filename or ""
    suffix = Path(filename).suffix.lower()
    if kind == "logo":
        if file.content_type != "image/png" or suffix != ".png":
            raise HTTPException(status_code=400, detail="Logo must be a PNG file")
        if len(content) > MAX_LOGO_BYTES:
            raise HTTPException(status_code=400, detail="Logo exceeds 2 MB limit")
        return "logo.png"

    if suffix not in {".png", ".ico"}:
        raise HTTPException(status_code=400, detail="Favicon must be PNG or ICO")
    if file.content_type not in {"image/png", "image/x-icon", "image/vnd.microsoft.icon", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Unsupported favicon content type")
    if len(content) > MAX_FAVICON_BYTES:
        raise HTTPException(status_code=400, detail="Favicon exceeds 512 KB limit")
    return f"favicon{suffix}"


def _sanitize_skin_slug(filename: str) -> str:
    stem = Path(filename).stem.strip().lower()
    stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    return stem or "instance-skin"


def _tui_skin_path(filename: str) -> Path:
    return settings.hermes_skins_root / filename


async def _resync_global_tui_skin(request: Request, db: AsyncSession) -> None:
    agents = (await db.execute(select(Agent).where(Agent.is_archived.is_(False)))).scalars().all()
    installation_manager = request.app.state.installation_manager
    for agent in agents:
        await installation_manager.sync_agent_installation(agent)
    pty_manager = request.app.state.pty_manager
    for agent_id in list(pty_manager.sessions.keys()):
        await pty_manager.destroy_session(agent_id)


@router.get("", response_model=AppSettingsRead)
async def get_settings(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AppSettingsRead:
    item = await _get_or_create_settings(db)
    return _settings_to_read(item)


@router.get("/public", response_model=PublicSettingsRead)
async def get_public_settings(
    db: AsyncSession = Depends(get_db_session),
) -> PublicSettingsRead:
    item = await _get_or_create_settings(db)
    return _settings_to_public_read(item)


@router.put("", response_model=AppSettingsRead)
async def update_settings(
    request: Request,
    payload: AppSettingsUpdate,
    admin_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AppSettingsRead:
    if payload.default_hermes_version == "bundled":
        payload.default_hermes_version = None
    if payload.default_hermes_version:
        if not request.app.state.hermes_version_manager.is_installed(payload.default_hermes_version):
            raise HTTPException(
                status_code=400,
                detail=f"Hermes version '{payload.default_hermes_version}' is not installed",
            )
    item = await _get_or_create_settings(db)
    changes = payload.model_dump(exclude_unset=True)
    if "resend_api_key" in changes:
        resend_value = changes["resend_api_key"]
        if resend_value == MASKED_RESEND_API_KEY:
            changes.pop("resend_api_key")
        elif resend_value:
            changes["resend_api_key"] = encrypt_value(request.app.state.secret_vault, str(resend_value))
        else:
            changes["resend_api_key"] = None
    old_snapshot = {k: getattr(item, k, None) for k in changes}
    for field, value in changes.items():
        setattr(item, field, value)
    await record_audit(
        db,
        action="settings.update",
        target_type="settings",
        target_id="default",
        actor_id=admin_user.id,
        actor_username=admin_user.username,
        actor_role=admin_user.role,
        ip_address=extract_ip(request),
        old_value=redact_sensitive(old_snapshot),
        new_value=redact_sensitive(changes),
    )
    await db.commit()
    await db.refresh(item)
    return _settings_to_read(item)


@router.post("/branding/logo", response_model=AppSettingsRead)
async def upload_logo(
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AppSettingsRead:
    item = await _get_or_create_settings(db)
    content = await file.read()
    target_name = _validate_upload("logo", file, content)
    settings.branding_root.mkdir(parents=True, exist_ok=True)
    target_path = _branding_path(target_name)
    async with aiofiles.open(target_path, "wb") as f:
        await f.write(content)
    item.logo_filename = target_name
    await db.commit()
    await db.refresh(item)
    return _settings_to_read(item)


@router.post("/branding/favicon", response_model=AppSettingsRead)
async def upload_favicon(
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AppSettingsRead:
    item = await _get_or_create_settings(db)
    content = await file.read()
    target_name = _validate_upload("favicon", file, content)
    settings.branding_root.mkdir(parents=True, exist_ok=True)
    for old_name in ("favicon.png", "favicon.ico"):
        old_path = _branding_path(old_name)
        if old_path.exists() and old_name != target_name:
            old_path.unlink()
    target_path = _branding_path(target_name)
    async with aiofiles.open(target_path, "wb") as f:
        await f.write(content)
    item.favicon_filename = target_name
    await db.commit()
    await db.refresh(item)
    return _settings_to_read(item)


@router.post("/tui-skin", response_model=AppSettingsRead)
async def upload_tui_skin(
    request: Request,
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AppSettingsRead:
    item = await _get_or_create_settings(db)
    content = await file.read()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".yaml", ".yml"}:
        raise HTTPException(status_code=400, detail="TUI skin must be a YAML file")
    if len(content) > MAX_TUI_SKIN_BYTES:
        raise HTTPException(status_code=400, detail="TUI skin exceeds 256 KB limit")
    try:
        parsed = yaml.safe_load(content.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail="Invalid YAML skin file") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Skin YAML must define a mapping object")

    settings.hermes_skins_root.mkdir(parents=True, exist_ok=True)
    skin_slug = _sanitize_skin_slug(file.filename or "")
    target_name = f"{MANAGED_SKIN_PREFIX}{skin_slug}.yaml"
    if item.tui_skin_filename and item.tui_skin_filename != target_name:
        old_path = _tui_skin_path(item.tui_skin_filename)
        if old_path.exists():
            old_path.unlink()
    async with aiofiles.open(_tui_skin_path(target_name), "wb") as f:
        await f.write(content)
    item.default_tui_skin = skin_slug
    item.tui_skin_filename = target_name
    await db.commit()
    await db.refresh(item)
    await _resync_global_tui_skin(request, db)
    await db.refresh(item)
    return _settings_to_read(item)


@router.delete("/tui-skin", response_model=AppSettingsRead)
async def delete_tui_skin(
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AppSettingsRead:
    item = await _get_or_create_settings(db)
    if item.tui_skin_filename:
        path = _tui_skin_path(item.tui_skin_filename)
        if path.exists():
            path.unlink()
    item.default_tui_skin = None
    item.tui_skin_filename = None
    await db.commit()
    await db.refresh(item)
    await _resync_global_tui_skin(request, db)
    await db.refresh(item)
    return _settings_to_read(item)


@router.delete("/branding/logo", response_model=AppSettingsRead)
async def delete_logo(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AppSettingsRead:
    item = await _get_or_create_settings(db)
    if item.logo_filename:
        path = _branding_path(item.logo_filename)
        if path.exists():
            path.unlink()
        item.logo_filename = None
        await db.commit()
        await db.refresh(item)
    return _settings_to_read(item)


@router.delete("/branding/favicon", response_model=AppSettingsRead)
async def delete_favicon(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AppSettingsRead:
    item = await _get_or_create_settings(db)
    if item.favicon_filename:
        path = _branding_path(item.favicon_filename)
        if path.exists():
            path.unlink()
        item.favicon_filename = None
        await db.commit()
        await db.refresh(item)
    return _settings_to_read(item)


@router.get("/branding/logo")
async def get_logo(
    db: AsyncSession = Depends(get_db_session),
):
    item = await _get_or_create_settings(db)
    if not item.logo_filename:
        raise HTTPException(status_code=404, detail="Logo not configured")
    path = _branding_path(item.logo_filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Logo asset missing")
    return FileResponse(path, media_type="image/png")


@router.get("/branding/favicon")
async def get_favicon(
    db: AsyncSession = Depends(get_db_session),
):
    item = await _get_or_create_settings(db)
    if not item.favicon_filename:
        raise HTTPException(status_code=404, detail="Favicon not configured")
    path = _branding_path(item.favicon_filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Favicon asset missing")
    media_type, _ = mimetypes.guess_type(path.name)
    if path.suffix.lower() == ".ico":
        media_type = "image/x-icon"
    return FileResponse(path, media_type=media_type or "application/octet-stream")


# ── Resource endpoints ────────────────────────────────────────────────────────


@router.get("/resources", response_model=ResourceStatusResponse)
async def get_resource_status(
    _: User = Depends(require_admin),
) -> ResourceStatusResponse:
    """Current resource status: semaphore, container, system, estimate."""
    limits = resource_monitor.get_container_limits()
    usage = resource_monitor.get_container_usage()
    system = resource_monitor.get_system_resources()

    # Get active task count from app state
    active_count = 0
    try:
        from sqlalchemy import func, select

        from hermeshq.database import AsyncSessionLocal
        from hermeshq.models.task import Task

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(func.count()).where(Task.status == "running"))
            active_count = result.scalar() or 0
    except Exception:  # noqa: BLE001  # DB query best-effort for status
        logger.warning("Failed to count running tasks for resource status", exc_info=True)

    semaphore_info = resource_monitor.get_semaphore_info(active_count)

    container = {
        "memory_limit_mb": limits.get("memory_limit_mb"),
        "memory_usage_mb": usage.get("memory_mb"),
        "cpu_limit": limits.get("cpu_limit"),
        "cpu_usage_pct": usage.get("cpu_pct"),
    }

    return ResourceStatusResponse(
        semaphore=semaphore_info,
        container=container,
        system=system,
        estimate=None,
    )


@router.put("/resources/semaphore", response_model=SemaphoreUpdateResponse)
async def update_semaphore(
    payload: SemaphoreUpdateRequest,
    _: User = Depends(require_admin),
) -> SemaphoreUpdateResponse:
    """Update the concurrency semaphore in .env (requires restart)."""
    if payload.semaphore < 1:
        raise HTTPException(status_code=400, detail="Semaphore must be at least 1")
    if payload.semaphore > 200:
        raise HTTPException(status_code=400, detail="Semaphore cannot exceed 200")

    env_path = Path(settings.model_config.get("env_file", ".env"))
    if not env_path.is_absolute():
        env_path = Path(__file__).resolve().parents[2] / env_path

    lines: list[str] = []
    found = False
    if env_path.exists():
        async with aiofiles.open(env_path) as f:
            text = await f.read()
        for line in text.splitlines():
            if line.strip().startswith("CONCURRENCY_SEMAPHORE="):
                lines.append(f"CONCURRENCY_SEMAPHORE={payload.semaphore}")
                found = True
            else:
                lines.append(line)

    if not found:
        lines.append(f"CONCURRENCY_SEMAPHORE={payload.semaphore}")

    async with aiofiles.open(env_path, "w") as f:
        await f.write("\n".join(lines) + "\n")

    # Also update runtime value immediately (no restart needed for semaphore)
    from hermeshq.config import update_runtime_setting

    update_runtime_setting("concurrency_semaphore", payload.semaphore)

    # Also update the agent supervisor semaphore in-place
    try:
        from hermeshq.services.agent_supervisor import get_supervisor

        supervisor = get_supervisor()
        await supervisor.update_semaphore(payload.semaphore)
    except Exception:  # noqa: BLE001  # supervisor update best-effort
        logger.warning("Failed to update concurrency semaphore", exc_info=True)

    logging.getLogger(__name__).info(
        "CONCURRENCY_SEMAPHORE updated to %d (applied immediately + persisted)",
        payload.semaphore,
    )

    return SemaphoreUpdateResponse(
        semaphore=payload.semaphore,
        restart_required=False,
    )


@router.post("/resources/generate-override", response_model=GenerateOverrideResponse)
async def generate_docker_override(
    payload: GenerateOverrideRequest,
    _: User = Depends(require_admin),
) -> GenerateOverrideResponse:
    """Generate a docker-compose.override.yml for the given agent count."""
    if payload.agents < 1:
        raise HTTPException(status_code=400, detail="Agent count must be at least 1")
    if payload.agents > 200:
        raise HTTPException(status_code=400, detail="Agent count cannot exceed 200")

    sizing = resource_monitor.calculate_sizing(payload.agents)

    ram_backend_mb = sizing["ram_backend_mb"]
    ram_postgres_mb = sizing["ram_postgres_mb"]
    cpu_backend = max(0.5, sizing["cpu_needed"] * 0.75)
    cpu_postgres = max(0.5, sizing["cpu_needed"] * 0.25)
    pg_shared_buffers = ram_postgres_mb // 4
    pg_effective_cache = ram_postgres_mb * 3 // 4
    pg_max_connections = max(50, sizing["semaphore"] * 2)

    content = f"""# docker-compose.override.yml — generated by HermesHQ
# For {payload.agents} agents ({sizing["concurrent"]} concurrent)
services:
  postgres:
    command: >
      postgres
        -c shared_buffers={pg_shared_buffers}MB
        -c max_connections={pg_max_connections}
        -c work_mem=64MB
        -c effective_cache_size={pg_effective_cache}MB
    deploy:
      resources:
        limits:
          memory: {ram_postgres_mb}M
          cpus: '{cpu_postgres:.1f}'
  backend:
    deploy:
      resources:
        limits:
          memory: {ram_backend_mb}M
          cpus: '{cpu_backend:.1f}'
  frontend:
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: '0.5'
"""

    return GenerateOverrideResponse(
        content=content,
        agents=payload.agents,
        semaphore=sizing["semaphore"],
        applied=False,
        restart_required=True,
    )
