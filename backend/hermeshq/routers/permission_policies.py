from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.permission_policy import PermissionPolicy
from hermeshq.models.user import User
from hermeshq.schemas.permission_policy import (
    PermissionPolicyCreate,
    PermissionPolicyRead,
    PermissionPolicyUpdate,
)

router = APIRouter(prefix="/permission-policies", tags=["permission-policies"])


@router.get("", response_model=list[PermissionPolicyRead])
async def list_policies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[PermissionPolicy]:
    result = await db.execute(
        select(PermissionPolicy).order_by(PermissionPolicy.is_system.desc(), PermissionPolicy.name.asc())
    )
    return list(result.scalars().all())


@router.post("", response_model=PermissionPolicyRead, status_code=201)
async def create_policy(
    payload: PermissionPolicyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PermissionPolicy:
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    policy = PermissionPolicy(
        name=payload.name,
        description=payload.description,
        tool_rules=payload.tool_rules,
        path_rules=payload.path_rules,
        command_rules=payload.command_rules,
        network_rules=payload.network_rules,
        approval_rules=payload.approval_rules,
    )
    db.add(policy)
    await db.commit()
    await db.refresh(policy)
    return policy


@router.get("/{policy_id}", response_model=PermissionPolicyRead)
async def get_policy(
    policy_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PermissionPolicy:
    policy = await db.get(PermissionPolicy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    return policy


@router.put("/{policy_id}", response_model=PermissionPolicyRead)
async def update_policy(
    policy_id: str,
    payload: PermissionPolicyUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> PermissionPolicy:
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    policy = await db.get(PermissionPolicy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(policy, field, value)
    await db.commit()
    await db.refresh(policy)
    return policy


@router.delete("/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin only")
    policy = await db.get(PermissionPolicy, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    if policy.is_system:
        raise HTTPException(status_code=400, detail="System policies cannot be deleted")
    await db.delete(policy)
    await db.commit()
