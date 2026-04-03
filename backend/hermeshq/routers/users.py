from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import hash_password, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.user import User
from hermeshq.schemas.user_management import UserCreate, UserManagedRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


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


async def _to_read(db: AsyncSession, user: User) -> UserManagedRead:
    return UserManagedRead(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        assigned_agent_ids=await _load_assigned_agent_ids(db, user.id),
    )


@router.get("", response_model=list[UserManagedRead])
async def list_users(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> list[UserManagedRead]:
    result = await db.execute(select(User).order_by(User.created_at.asc()))
    users = result.scalars().all()
    return [await _to_read(db, user) for user in users]


@router.post("", response_model=UserManagedRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
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
    return await _to_read(db, user)


@router.put("/{user_id}", response_model=UserManagedRead)
async def update_user(
    user_id: str,
    payload: UserUpdate,
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
    return await _to_read(db, user)

