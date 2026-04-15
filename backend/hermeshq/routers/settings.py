import mimetypes
import re
from pathlib import Path

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
from hermeshq.schemas.settings import AppSettingsRead, AppSettingsUpdate
from hermeshq.versioning import get_app_version

router = APIRouter(prefix="/settings", tags=["settings"])
settings = get_settings()
MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_FAVICON_BYTES = 512 * 1024
MAX_TUI_SKIN_BYTES = 256 * 1024
MANAGED_SKIN_PREFIX = "hermeshq-global-"


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
        tui_skin_filename=item.tui_skin_filename,
        logo_url=logo_url,
        favicon_url=favicon_url,
        has_tui_skin=bool(item.tui_skin_filename and item.default_tui_skin),
        has_logo=bool(item.logo_filename),
        has_favicon=bool(item.favicon_filename),
        created_at=item.created_at,
        updated_at=item.updated_at,
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


@router.get("/public", response_model=AppSettingsRead)
async def get_public_settings(
    db: AsyncSession = Depends(get_db_session),
) -> AppSettingsRead:
    item = await _get_or_create_settings(db)
    return _settings_to_read(item)


@router.put("", response_model=AppSettingsRead)
async def update_settings(
    request: Request,
    payload: AppSettingsUpdate,
    _: User = Depends(require_admin),
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
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
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
    target_path.write_bytes(content)
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
    target_path.write_bytes(content)
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
    except Exception as exc:
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
    _tui_skin_path(target_name).write_bytes(content)
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
