from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.core.security import get_current_user, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.cloud import CloudBinding, CloudPlatform, CloudTenant
from hermeshq.models.user import User
from hermeshq.schemas.cloud import (
    CloudBindingCreate,
    CloudBindingRead,
    CloudPlatformCreate,
    CloudPlatformRead,
    CloudPlatformUpdate,
    CloudTenantCreate,
    CloudTenantRead,
    CloudTenantUpdate,
)
from hermeshq.services.audit import record_audit
from hermeshq.services.secret_vault import build_vault_from_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cloud", tags=["cloud"])


async def _upsert_secret(name: str, value: str | None) -> str:
    from hermeshq.database import AsyncSessionLocal
    from hermeshq.models.secret import Secret
    from hermeshq.services.secret_vault import encrypt_value

    if value is None:
        return name
    vault = build_vault_from_settings(get_settings())
    encrypted = encrypt_value(vault, value)
    async with AsyncSessionLocal() as session:
        existing = (await session.execute(select(Secret).where(Secret.name == name))).scalars().first()
        if existing is None:
            session.add(Secret(id=str(uuid4()), name=name, provider="cloud", value_enc=encrypted.encode()))
        else:
            existing.value_enc = encrypted.encode()
        await session.commit()
    return name


# ---------------- platforms ----------------


@router.get("/platforms", response_model=list[CloudPlatformRead])
async def list_platforms(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[CloudPlatformRead]:
    result = await db.execute(select(CloudPlatform).order_by(CloudPlatform.created_at.asc()))
    return [CloudPlatformRead.model_validate(item) for item in result.scalars().all()]


@router.post("/platforms", response_model=CloudPlatformRead, status_code=201)
async def create_platform(
    payload: CloudPlatformCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> CloudPlatformRead:
    item = CloudPlatform(
        id=str(uuid4()),
        kind=payload.kind,
        name=payload.name.strip(),
        api_url=payload.api_url,
        admin_credential_ref=payload.admin_credential_ref,
        insecure_tls=payload.insecure_tls,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    await record_audit(
        db,
        action="cloud_platform.created",
        target_type="cloud_platform",
        target_id=item.id,
        details={"name": item.name, "api_url": item.api_url},
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )
    return CloudPlatformRead.model_validate(item)


@router.put("/platforms/{platform_id}", response_model=CloudPlatformRead)
async def update_platform(
    platform_id: str,
    payload: CloudPlatformUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> CloudPlatformRead:
    item = await db.get(CloudPlatform, platform_id)
    if not item:
        raise HTTPException(status_code=404, detail="Platform not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    await db.commit()
    await db.refresh(item)
    await record_audit(
        db,
        action="cloud_platform.updated",
        target_type="cloud_platform",
        target_id=item.id,
        details={"changes": payload.model_dump(exclude_unset=True)},
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )
    return CloudPlatformRead.model_validate(item)


@router.delete("/platforms/{platform_id}", status_code=204)
async def delete_platform(
    platform_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    item = await db.get(CloudPlatform, platform_id)
    if not item:
        raise HTTPException(status_code=404, detail="Platform not found")
    await db.delete(item)
    await db.commit()
    await record_audit(
        db,
        action="cloud_platform.deleted",
        target_type="cloud_platform",
        target_id=platform_id,
        details={"name": item.name},
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )


# ---------------- tenants ----------------


@router.get("/tenants", response_model=list[CloudTenantRead])
async def list_tenants(
    platform_id: str | None = None,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[CloudTenantRead]:
    statement = select(CloudTenant).order_by(CloudTenant.created_at.asc())
    if platform_id:
        statement = statement.where(CloudTenant.platform_id == platform_id)
    result = await db.execute(statement)
    return [CloudTenantRead.model_validate(item) for item in result.scalars().all()]


@router.post("/tenants", response_model=CloudTenantRead, status_code=201)
async def create_tenant(
    payload: CloudTenantCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> CloudTenantRead:
    platform = await db.get(CloudPlatform, payload.platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail="Platform not found")
    credential_ref = payload.credential_ref
    if payload.api_key:
        secret_name = f"cloud-tenant-{uuid4().hex[:12]}"
        await _upsert_secret(secret_name, payload.api_key)
        credential_ref = secret_name
    item = CloudTenant(
        id=str(uuid4()),
        platform_id=payload.platform_id,
        tenant_ref=payload.tenant_ref.strip(),
        display_name=payload.display_name.strip(),
        credential_ref=credential_ref,
        cache_ttl_seconds=payload.cache_ttl_seconds,
    )
    db.add(item)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Tenant ref already exists on this platform") from exc
    await db.refresh(item)
    await record_audit(
        db,
        action="cloud_tenant.created",
        target_type="cloud_tenant",
        target_id=item.id,
        details={"display_name": item.display_name, "platform_id": item.platform_id},
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )
    return CloudTenantRead.model_validate(item)


@router.put("/tenants/{tenant_id}", response_model=CloudTenantRead)
async def update_tenant(
    tenant_id: str,
    payload: CloudTenantUpdate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> CloudTenantRead:
    item = await db.get(CloudTenant, tenant_id)
    if not item:
        raise HTTPException(status_code=404, detail="Tenant not found")
    changes = payload.model_dump(exclude_unset=True)
    api_key = changes.pop("api_key", None)
    for field, value in changes.items():
        setattr(item, field, value)
    if api_key:
        await _upsert_secret(item.credential_ref, api_key)
    await db.commit()
    await db.refresh(item)
    await record_audit(
        db,
        action="cloud_tenant.updated",
        target_type="cloud_tenant",
        target_id=item.id,
        details={"changes": {k: v for k, v in changes.items() if k != "api_key"}, "api_key_rotated": bool(api_key)},
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )
    return CloudTenantRead.model_validate(item)


@router.delete("/tenants/{tenant_id}", status_code=204)
async def delete_tenant(
    tenant_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    item = await db.get(CloudTenant, tenant_id)
    if not item:
        raise HTTPException(status_code=404, detail="Tenant not found")
    await db.delete(item)
    await db.commit()
    await record_audit(
        db,
        action="cloud_tenant.deleted",
        target_type="cloud_tenant",
        target_id=tenant_id,
        details={"display_name": item.display_name},
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )


# ---------------- bindings ----------------


@router.get("/bindings", response_model=list[CloudBindingRead])
async def list_bindings(
    agent_id: str | None = None,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[CloudBindingRead]:
    statement = select(CloudBinding).order_by(CloudBinding.created_at.asc())
    if agent_id:
        statement = statement.where(CloudBinding.agent_id == agent_id)
    result = await db.execute(statement)
    return [CloudBindingRead.model_validate(item) for item in result.scalars().all()]


@router.post("/bindings", response_model=CloudBindingRead, status_code=201)
async def create_binding(
    payload: CloudBindingCreate,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> CloudBindingRead:
    tenant = await db.get(CloudTenant, payload.tenant_id)
    if not tenant or not tenant.active:
        raise HTTPException(status_code=404, detail="Tenant not found")
    agent = await db.get(Agent, payload.agent_id)
    if not agent or agent.is_archived:
        raise HTTPException(status_code=404, detail="Agent not found")
    item = CloudBinding(
        id=str(uuid4()),
        tenant_id=payload.tenant_id,
        agent_id=payload.agent_id,
        cloud_role=payload.cloud_role,
    )
    db.add(item)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Binding already exists for this tenant/agent") from exc
    await db.refresh(item)
    await record_audit(
        db,
        action="cloud_binding.created",
        target_type="cloud_binding",
        target_id=item.id,
        details={"agent_id": payload.agent_id, "tenant_id": payload.tenant_id, "role": payload.cloud_role},
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )
    return CloudBindingRead.model_validate(item)


@router.delete("/bindings/{binding_id}", status_code=204)
async def delete_binding(
    binding_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    item = await db.get(CloudBinding, binding_id)
    if not item:
        raise HTTPException(status_code=404, detail="Binding not found")
    await db.delete(item)
    await db.commit()
    await record_audit(
        db,
        action="cloud_binding.deleted",
        target_type="cloud_binding",
        target_id=binding_id,
        details={"agent_id": item.agent_id, "tenant_id": item.tenant_id},
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )
