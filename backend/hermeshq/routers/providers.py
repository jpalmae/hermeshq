from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.provider import ProviderDefinition
from hermeshq.models.user import User
from hermeshq.schemas.provider import ProviderRead, ProviderUpdate

router = APIRouter(prefix="/providers", tags=["providers"])


@router.get("", response_model=list[ProviderRead])
async def list_providers(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[ProviderRead]:
    result = await db.execute(select(ProviderDefinition).order_by(ProviderDefinition.sort_order.asc(), ProviderDefinition.name.asc()))
    return [ProviderRead.model_validate(item) for item in result.scalars().all()]


@router.put("/{provider_slug}", response_model=ProviderRead)
async def update_provider(
    provider_slug: str,
    payload: ProviderUpdate,
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
    return ProviderRead.model_validate(item)
