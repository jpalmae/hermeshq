from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.integration_draft import IntegrationDraft
from hermeshq.models.user import User
from hermeshq.routers import integration_packages as integration_packages_router
from hermeshq.schemas.integration_factory import (
    IntegrationDraftCreate,
    IntegrationDraftFileContentRead,
    IntegrationDraftFileUpdate,
    IntegrationDraftPublishRead,
    IntegrationDraftRead,
    IntegrationDraftUpdate,
    IntegrationDraftValidationRead,
)
from hermeshq.schemas.managed_integration import ManagedIntegrationRead
from hermeshq.services.integration_factory import (
    build_draft_read,
    create_draft_files,
    delete_draft_files,
    normalize_draft_slug,
    publish_draft_package,
    read_draft_file,
    remove_draft_file,
    update_draft_metadata,
    validate_draft,
    write_draft_file,
)

router = APIRouter(prefix="/integration-factory", tags=["integration-factory"])


async def _get_draft_or_404(db: AsyncSession, draft_id: str) -> IntegrationDraft:
    draft = await db.get(IntegrationDraft, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Integration draft not found")
    return draft


@router.get("/drafts", response_model=list[IntegrationDraftRead])
async def list_integration_drafts(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> list[IntegrationDraftRead]:
    result = await db.execute(select(IntegrationDraft).order_by(IntegrationDraft.updated_at.desc()))
    return [build_draft_read(item) for item in result.scalars().all()]


@router.post("/drafts", response_model=IntegrationDraftRead, status_code=status.HTTP_201_CREATED)
async def create_integration_draft(
    payload: IntegrationDraftCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftRead:
    slug = normalize_draft_slug(payload.slug)
    existing = await db.execute(select(IntegrationDraft).where(IntegrationDraft.slug == slug))
    if existing.scalars().first():
        raise HTTPException(status_code=409, detail=f"Integration draft '{slug}' already exists")

    draft = IntegrationDraft(
        slug=slug,
        template=payload.template,
        status="draft",
        created_by_user_id=current_user.id,
    )
    db.add(draft)
    await db.flush()
    try:
        create_draft_files(draft, payload.model_copy(update={"slug": slug}))
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(draft)
    return build_draft_read(draft)


@router.get("/drafts/{draft_id}", response_model=IntegrationDraftRead)
async def get_integration_draft(
    draft_id: str,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftRead:
    draft = await _get_draft_or_404(db, draft_id)
    return build_draft_read(draft)


@router.put("/drafts/{draft_id}", response_model=IntegrationDraftRead)
async def update_integration_draft(
    draft_id: str,
    payload: IntegrationDraftUpdate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftRead:
    draft = await _get_draft_or_404(db, draft_id)
    try:
        update_draft_metadata(draft, payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    draft.status = "draft"
    draft.last_validation = None
    await db.commit()
    await db.refresh(draft)
    return build_draft_read(draft)


@router.delete("/drafts/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration_draft(
    draft_id: str,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    draft = await _get_draft_or_404(db, draft_id)
    delete_draft_files(draft)
    await db.delete(draft)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/drafts/{draft_id}/file", response_model=IntegrationDraftFileContentRead)
async def get_integration_draft_file(
    draft_id: str,
    path: str = Query(..., min_length=1),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftFileContentRead:
    draft = await _get_draft_or_404(db, draft_id)
    try:
        return read_draft_file(draft, path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Draft file '{path}' was not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/drafts/{draft_id}/file", response_model=IntegrationDraftRead)
async def put_integration_draft_file(
    draft_id: str,
    payload: IntegrationDraftFileUpdate,
    path: str = Query(..., min_length=1),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftRead:
    draft = await _get_draft_or_404(db, draft_id)
    try:
        write_draft_file(draft, path, payload.content)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    draft.status = "draft"
    draft.last_validation = None
    await db.commit()
    await db.refresh(draft)
    return build_draft_read(draft)


@router.delete("/drafts/{draft_id}/file", response_model=IntegrationDraftRead)
async def delete_integration_draft_file(
    draft_id: str,
    path: str = Query(..., min_length=1),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftRead:
    draft = await _get_draft_or_404(db, draft_id)
    try:
        remove_draft_file(draft, path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    draft.status = "draft"
    draft.last_validation = None
    await db.commit()
    await db.refresh(draft)
    return build_draft_read(draft)


@router.post("/drafts/{draft_id}/validate", response_model=IntegrationDraftValidationRead)
async def validate_integration_draft(
    draft_id: str,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftValidationRead:
    draft = await _get_draft_or_404(db, draft_id)
    validation = validate_draft(draft)
    draft.last_validation = validation.model_dump()
    draft.status = "validated" if validation.valid else "invalid"
    await db.commit()
    return validation


@router.post("/drafts/{draft_id}/publish", response_model=IntegrationDraftPublishRead)
async def publish_integration_draft(
    draft_id: str,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftPublishRead:
    draft = await _get_draft_or_404(db, draft_id)
    try:
        package = publish_draft_package(draft)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    settings = await db.get(AppSettings, "default")
    if not settings:
        settings = AppSettings(id="default")
        db.add(settings)
    enabled = [slug for slug in (settings.enabled_integration_packages or []) if isinstance(slug, str)]
    if package["slug"] not in enabled:
        enabled.append(package["slug"])
    settings.enabled_integration_packages = enabled

    await integration_packages_router._sync_agents_for_package(request, db, package, installing=True)
    package["installed"] = True

    validation = validate_draft(draft)
    draft.last_validation = validation.model_dump()
    draft.status = "published"
    draft.published_package_slug = package["slug"]
    draft.published_package_version = str(package.get("version") or "")
    draft.published_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(draft)
    return IntegrationDraftPublishRead(
        draft=build_draft_read(draft),
        integration=ManagedIntegrationRead.model_validate(package),
    )
