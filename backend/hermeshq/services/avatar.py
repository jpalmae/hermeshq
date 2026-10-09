"""Shared avatar upload, validation, and file management service.

Consolidates avatar logic previously duplicated across routers (auth, agents, users).
"""

from io import BytesIO
from pathlib import Path

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

ALLOWED_AVATAR_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
MAX_AVATAR_BYTES: int = 2 * 1024 * 1024  # 2 MB
MAX_AVATAR_PIXELS: int = 16_777_216
AVATAR_IMAGE_FORMATS: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
}

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


def _validate_raster_bytes(content: bytes, content_type: str) -> None:
    try:
        with Image.open(BytesIO(content)) as image:
            if image.format != AVATAR_IMAGE_FORMATS.get(content_type):
                raise ValueError("Image format does not match content type")
            if image.width * image.height > MAX_AVATAR_PIXELS:
                raise ValueError("Image dimensions exceed the avatar limit")
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(status_code=400, detail="Avatar content is not a valid image")


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

    content = await file.read(MAX_AVATAR_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="Avatar file is empty")
    if len(content) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="Avatar exceeds 2 MB limit")

    _validate_raster_bytes(content, file.content_type)

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


def save_avatar_bytes(
    base_path: Path,
    entity_id: str,
    content: bytes,
    content_type: str = "image/png",
) -> str:
    """Save raw bytes as an avatar file (no UploadFile dependency).

    Returns the filename (e.g. ``avatar.png``) of the saved file.
    """
    if content_type not in ALLOWED_AVATAR_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported avatar type")
    if not content or len(content) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="Invalid avatar size")
    _validate_raster_bytes(content, content_type)
    extension = ALLOWED_AVATAR_TYPES[content_type]
    avatar_dir = build_avatar_dir(base_path, entity_id)
    avatar_dir.mkdir(parents=True, exist_ok=True)

    # Remove any existing avatar files before saving the new one
    for existing in avatar_dir.iterdir():
        if existing.is_file() or existing.is_symlink():
            existing.unlink()

    filename = f"avatar{extension}"
    (avatar_dir / filename).write_bytes(content)
    return filename


def resolve_media_type(avatar_path: Path) -> str:
    """Return the MIME type for an avatar path based on extension."""
    return AVATAR_MEDIA_TYPES.get(avatar_path.suffix.lower(), "application/octet-stream")
