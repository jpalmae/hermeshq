"""Shared avatar upload, validation, and file management service.

Consolidates avatar logic previously duplicated across routers (auth, agents, users).
"""

from pathlib import Path

from fastapi import HTTPException, UploadFile

ALLOWED_AVATAR_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
MAX_AVATAR_BYTES: int = 2 * 1024 * 1024  # 2 MB

AVATAR_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def get_assets_root(base_path: Path) -> Path:
    """Ensure and return the root directory for avatar assets."""
    base_path.mkdir(parents=True, exist_ok=True)
    return base_path


def build_avatar_dir(base_path: Path, entity_id: str) -> Path:
    """Return the per-entity avatar directory."""
    return get_assets_root(base_path) / entity_id


def build_avatar_path(base_path: Path, entity_id: str, avatar_filename: str | None) -> Path | None:
    """Return the full path to an avatar file, or None if not set."""
    if not avatar_filename:
        return None
    return build_avatar_dir(base_path, entity_id) / avatar_filename


def delete_avatar_files(base_path: Path, entity_id: str) -> None:
    """Remove the avatar directory and all contents for an entity."""
    avatar_dir = build_avatar_dir(base_path, entity_id)
    if not avatar_dir.exists():
        return
    for path in sorted(avatar_dir.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    avatar_dir.rmdir()


async def validate_and_save_avatar(
    base_path: Path,
    entity_id: str,
    file: UploadFile,
) -> str:
    """Validate an uploaded avatar file and persist it.

    Returns the filename (e.g. ``avatar.png``) of the saved file.

    Raises:
        HTTPException: On invalid type, empty file, or size exceeded.
    """
    if file.content_type not in ALLOWED_AVATAR_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported avatar type. Use PNG, JPG or WEBP.",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Avatar file is empty")
    if len(content) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="Avatar exceeds 2 MB limit")

    avatar_dir = build_avatar_dir(base_path, entity_id)
    avatar_dir.mkdir(parents=True, exist_ok=True)

    # Remove any existing avatar files before saving the new one
    for existing in avatar_dir.iterdir():
        if existing.is_file() or existing.is_symlink():
            existing.unlink()

    extension = ALLOWED_AVATAR_TYPES[file.content_type]
    filename = f"avatar{extension}"
    (avatar_dir / filename).write_bytes(content)
    return filename


def resolve_media_type(avatar_path: Path) -> str:
    """Return the MIME type for an avatar path based on extension."""
    return AVATAR_MEDIA_TYPES.get(avatar_path.suffix.lower(), "application/octet-stream")
