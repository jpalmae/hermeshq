from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from hermeshq.config import get_settings
from hermeshq.database import get_db_session
from hermeshq.models.user import User
from hermeshq.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    TokenResponse,
    UserPreferencesUpdate,
    UserProfileUpdate,
    UserRead,
)

router = APIRouter(prefix="/auth", tags=["auth"])
ALLOWED_AVATAR_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
MAX_AVATAR_BYTES = 2 * 1024 * 1024


def _get_user_assets_root() -> Path:
    root = Path(get_settings().user_assets_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _build_avatar_dir(user_id: str) -> Path:
    return _get_user_assets_root() / user_id


def _build_avatar_path(user: User) -> Path | None:
    if not user.avatar_filename:
        return None
    return _build_avatar_dir(user.id) / user.avatar_filename


def _delete_avatar_files(user_id: str) -> None:
    avatar_dir = _build_avatar_dir(user_id)
    if not avatar_dir.exists():
        return
    for path in sorted(avatar_dir.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    avatar_dir.rmdir()


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
    if payload.theme_preference is not None:
        if payload.theme_preference not in {"default", "dark", "light", "system", "enterprise"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid theme preference")
        current_user.theme_preference = payload.theme_preference
    if payload.locale_preference is not None:
        if payload.locale_preference not in {"default", "en", "es"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid locale preference")
        current_user.locale_preference = payload.locale_preference
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)


@router.put("/me/profile", response_model=UserRead)
async def update_profile(
    payload: UserProfileUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    current_user.display_name = payload.display_name
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)


@router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def update_my_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be different")
    current_user.password_hash = hash_password(payload.new_password)
    await db.commit()


@router.get("/me/avatar", include_in_schema=False)
async def get_my_avatar(current_user: User = Depends(get_current_user)):
    if not current_user.avatar_filename:
        raise HTTPException(status_code=404, detail="Avatar not found")
    avatar_path = _build_avatar_path(current_user)
    if not avatar_path or not avatar_path.exists():
        raise HTTPException(status_code=404, detail="Avatar not found")
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(avatar_path.suffix.lower(), "application/octet-stream")
    return FileResponse(avatar_path, media_type=media_type)


@router.post("/me/avatar", response_model=UserRead)
async def upload_my_avatar(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    if file.content_type not in ALLOWED_AVATAR_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported avatar type. Use PNG, JPG or WEBP.")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Avatar file is empty")
    if len(content) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="Avatar exceeds 2 MB limit")
    avatar_dir = _build_avatar_dir(current_user.id)
    avatar_dir.mkdir(parents=True, exist_ok=True)
    for existing in avatar_dir.iterdir():
        if existing.is_file() or existing.is_symlink():
            existing.unlink()
    extension = ALLOWED_AVATAR_TYPES[file.content_type]
    filename = f"avatar{extension}"
    (avatar_dir / filename).write_bytes(content)
    current_user.avatar_filename = filename
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)


@router.delete("/me/avatar", response_model=UserRead)
async def delete_my_avatar(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    _delete_avatar_files(current_user.id)
    current_user.avatar_filename = None
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)
