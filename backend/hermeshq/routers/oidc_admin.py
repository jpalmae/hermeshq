"""OIDC Provider admin router — CRUD for enterprise OIDC providers."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.oidc_provider import OidcProvider
from hermeshq.models.user import User
from hermeshq.schemas.oidc_provider import (
    OidcProviderCreate,
    OidcProviderRead,
    OidcProviderUpdate,
)

router = APIRouter(prefix="/oidc-providers", tags=["oidc-providers"])


@router.get("", response_model=list[OidcProviderRead])
async def list_providers(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> list[OidcProvider]:
    result = await db.execute(select(OidcProvider).order_by(OidcProvider.name))
    return list(result.scalars().all())


@router.post("", response_model=OidcProviderRead, status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: OidcProviderCreate,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> OidcProvider:
    existing = await db.execute(select(OidcProvider).where(OidcProvider.slug == payload.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Provider '{payload.slug}' already exists")
    provider = OidcProvider(**payload.model_dump())
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return provider


@router.get("/{provider_id}", response_model=OidcProviderRead)
async def get_provider(
    provider_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> OidcProvider:
    provider = await db.get(OidcProvider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider


@router.patch("/{provider_id}", response_model=OidcProviderRead)
async def update_provider(
    provider_id: str,
    payload: OidcProviderUpdate,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> OidcProvider:
    provider = await db.get(OidcProvider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(provider, key, value)
    await db.commit()
    await db.refresh(provider)
    return provider


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    provider = await db.get(OidcProvider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    await db.delete(provider)
    await db.commit()
