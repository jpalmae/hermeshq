import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.core.security import get_current_user, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.provider import ProviderDefinition
from hermeshq.models.user import User
from hermeshq.schemas.provider import ProviderRead, ProviderUpdate
from hermeshq.services.egress_allowlist import ensure_domain_allowed, push_allowlist_to_runner
from hermeshq.services.provider_models import refresh_provider_models
from hermeshq.services.secret_vault import build_vault_from_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("", response_model=list[ProviderRead])
async def list_providers(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[ProviderRead]:
    result = await db.execute(
        select(ProviderDefinition).order_by(ProviderDefinition.sort_order.asc(), ProviderDefinition.name.asc())
    )
    return [ProviderRead.model_validate(item) for item in result.scalars().all()]


@router.put("/{provider_slug}", response_model=ProviderRead)
async def update_provider(
    provider_slug: str,
    payload: ProviderUpdate,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> ProviderRead:
    item = await db.get(ProviderDefinition, provider_slug)
    if not item:
        raise HTTPException(status_code=404, detail="Provider not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    await db.commit()
    await db.refresh(item)
    notice: str | None = None
    if item.base_url and item.supports_custom_base_url:
        added, host = await ensure_domain_allowed(db, item.base_url)
        if host:
            client = getattr(request.app.state, "runtime_runner_client", None)
            if client is not None:
                try:
                    await push_allowlist_to_runner(client, db)
                except Exception:
                    logger.warning("Could not push egress allowlist to runtime runner for %s", host)
                    notice = (
                        f"El dominio {host} quedó guardado y se permitirá automáticamente, "
                        "pero el runtime runner no está accesible ahora — se sincronizará al reiniciar."
                    )
            if added and notice is None:
                notice = (
                    f"Dominio {host} agregado automáticamente a la allowlist de runtime-egress — "
                    "los agentes ya pueden conectarse (efectivo al instante, sin reinicios)."
                )
    read = ProviderRead.model_validate(item)
    read.egress_notice = notice
    return read


@router.post("/{provider_slug}/refresh-models")
async def refresh_models(
    provider_slug: str,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    provider = await db.get(ProviderDefinition, provider_slug)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    settings = await db.get(AppSettings, "default")
    vault = build_vault_from_settings(get_settings())

    try:
        models = await refresh_provider_models(db, provider, settings, vault)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "slug": provider.slug,
        "models": models,
        "count": len(models),
        "refreshed_at": provider.models_refreshed_at.isoformat() if provider.models_refreshed_at else None,
    }
