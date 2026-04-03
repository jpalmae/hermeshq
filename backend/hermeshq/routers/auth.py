from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import (
    create_access_token,
    get_current_user,
    verify_password,
)
from hermeshq.database import get_db_session
from hermeshq.models.user import User
from hermeshq.schemas.auth import LoginRequest, TokenResponse, UserPreferencesUpdate, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


def _serialize_user(request: Request, user: User) -> UserRead:
    payload = UserRead.model_validate(user)
    avatar_url = None
    if user.avatar_filename:
        version = int(user.updated_at.timestamp()) if user.updated_at else 0
        avatar_url = f"/api/users/{user.id}/avatar?v={version}"
    return payload.model_copy(update={"avatar_url": avatar_url, "has_avatar": bool(user.avatar_filename)})


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db_session)) -> TokenResponse:
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token, expires_at = create_access_token(user.username)
    return TokenResponse(access_token=token, expires_at=expires_at)


@router.get("/me", response_model=UserRead)
async def me(request: Request, current_user: User = Depends(get_current_user)) -> UserRead:
    return _serialize_user(request, current_user)


@router.put("/me/preferences", response_model=UserRead)
async def update_preferences(
    payload: UserPreferencesUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    if payload.theme_preference not in {"default", "dark", "light", "system"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid theme preference")
    current_user.theme_preference = payload.theme_preference
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)
