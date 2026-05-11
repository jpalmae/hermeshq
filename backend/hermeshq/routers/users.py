from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.core.security import hash_password, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.user import User
from hermeshq.schemas.user_management import UserCreate, UserManagedRead, UserUpdate
from hermeshq.services.avatar import (
    build_avatar_path as _build_avatar_path_shared,
    delete_avatar_files as _delete_avatar_files_shared,
    validate_and_save_avatar,
    resolve_media_type,
)

router = APIRouter(prefix="/users", tags=["users"])


def _user_avatar_base() -> Path:
    return Path(get_settings().user_assets_root)


def _build_avatar_path(user: User) -> Path | None:
    return _build_avatar_path_shared(_user_avatar_base(), user.id, user.avatar_filename)


async def _load_assigned_agent_ids(db: AsyncSession, user_id: str) -> list[str]:
    result = await db.execute(
        select(AgentAssignment.agent_id)
        .where(AgentAssignment.user_id == user_id)
        .order_by(AgentAssignment.created_at.asc())
    )
    return list(result.scalars().all())


async def _sync_assignments(
    db: AsyncSession,
    user: User,
    agent_ids: list[str],
    assigned_by: str | None,
) -> None:
    normalized_ids = list(dict.fromkeys(agent_ids))
    if normalized_ids:
        result = await db.execute(select(Agent.id).where(Agent.id.in_(normalized_ids)))
        existing_ids = set(result.scalars().all())
        missing_ids = [agent_id for agent_id in normalized_ids if agent_id not in existing_ids]
        if missing_ids:
            raise HTTPException(status_code=404, detail=f"Unknown agent ids: {', '.join(missing_ids)}")
    await db.execute(delete(AgentAssignment).where(AgentAssignment.user_id == user.id))
    for agent_id in normalized_ids:
        db.add(
            AgentAssignment(
                user_id=user.id,
                agent_id=agent_id,
                assigned_by=assigned_by,
            )
        )


def _serialize_user(request: Request, user: User, assigned_agent_ids: list[str]) -> UserManagedRead:
    avatar_url = None
    if user.avatar_filename:
        version = int(user.updated_at.timestamp()) if user.updated_at else 0
        avatar_url = f"/api/users/{user.id}/avatar?v={version}"
    return UserManagedRead(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        assigned_agent_ids=assigned_agent_ids,
        avatar_url=avatar_url,
        has_avatar=bool(user.avatar_filename),
    )


async def _to_read(request: Request, db: AsyncSession, user: User) -> UserManagedRead:
    return _serialize_user(request, user, await _load_assigned_agent_ids(db, user.id))


@router.get("", response_model=list[UserManagedRead])
async def list_users(
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> list[UserManagedRead]:
    result = await db.execute(select(User).order_by(User.created_at.asc()))
    users = result.scalars().all()
    return [await _to_read(request, db, user) for user in users]


@router.post("", response_model=UserManagedRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> UserManagedRead:
    existing = await db.execute(select(User).where(User.username == payload.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=payload.username,
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_active=payload.is_active,
    )
    db.add(user)
    await db.flush()
    await _sync_assignments(db, user, payload.assigned_agent_ids, current_user.id)
    await db.commit()
    await db.refresh(user)
    return await _to_read(request, db, user)


@router.put("/{user_id}", response_model=UserManagedRead)
async def update_user(
    user_id: str,
    payload: UserUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> UserManagedRead:
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.username == current_user.username and payload.role == "user":
        raise HTTPException(status_code=400, detail="You cannot demote the current admin session")
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        if user.username == current_user.username and not payload.is_active:
            raise HTTPException(status_code=400, detail="You cannot deactivate the current admin session")
        user.is_active = payload.is_active
    if payload.assigned_agent_ids is not None:
        await _sync_assignments(db, user, payload.assigned_agent_ids, current_user.id)
    await db.commit()
    await db.refresh(user)
    return await _to_read(request, db, user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.username == current_user.username:
        raise HTTPException(status_code=400, detail="You cannot delete the current admin session")
    _delete_avatar_files_shared(_user_avatar_base(), user_id)
    await db.delete(user)
    await db.commit()


@router.get("/{user_id}/avatar", include_in_schema=False)
async def get_user_avatar(user_id: str, db: AsyncSession = Depends(get_db_session)):
    user = await db.get(User, user_id)
    if not user or not user.avatar_filename:
        raise HTTPException(status_code=404, detail="Avatar not found")
    avatar_path = _build_avatar_path(user)
    if not avatar_path or not avatar_path.exists():
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(avatar_path, media_type=resolve_media_type(avatar_path))


@router.post("/{user_id}/avatar", response_model=UserManagedRead)
async def upload_user_avatar(
    user_id: str,
    request: Request,
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> UserManagedRead:
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.avatar_filename = await validate_and_save_avatar(_user_avatar_base(), user_id, file)
    await db.commit()
    await db.refresh(user)
    return await _to_read(request, db, user)


@router.delete("/{user_id}/avatar", response_model=UserManagedRead)
async def delete_user_avatar(
    user_id: str,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> UserManagedRead:
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _delete_avatar_files_shared(_user_avatar_base(), user_id)
    user.avatar_filename = None
    await db.commit()
    await db.refresh(user)
    return await _to_read(request, db, user)
