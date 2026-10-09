"""Unit tests for hermeshq.services.avatar."""

import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import HTTPException, UploadFile
from PIL import Image

from hermeshq.services.avatar import (
    ALLOWED_AVATAR_TYPES,
    AVATAR_MEDIA_TYPES,
    MAX_AVATAR_BYTES,
    build_avatar_dir,
    build_avatar_path,
    delete_avatar_files,
    get_assets_root,
    resolve_media_type,
    save_avatar_bytes,
    validate_and_save_avatar,
)

# ---------------------------------------------------------------------------
# resolve_media_type
# ---------------------------------------------------------------------------


class TestResolveMediaType(unittest.TestCase):
    """Tests for resolve_media_type()."""

    def test_png(self) -> None:
        self.assertEqual(resolve_media_type(Path("avatar.png")), "image/png")

    def test_jpg(self) -> None:
        self.assertEqual(resolve_media_type(Path("avatar.jpg")), "image/jpeg")

    def test_webp(self) -> None:
        self.assertEqual(resolve_media_type(Path("avatar.webp")), "image/webp")

    def test_svg(self) -> None:
        self.assertEqual(resolve_media_type(Path("avatar.svg")), "application/octet-stream")

    def test_unknown_returns_octet_stream(self) -> None:
        self.assertEqual(resolve_media_type(Path("avatar.unknown")), "application/octet-stream")

    def test_uppercase_extension_is_case_insensitive(self) -> None:
        self.assertEqual(resolve_media_type(Path("avatar.PNG")), "image/png")

    def test_mixed_case_extension(self) -> None:
        self.assertEqual(resolve_media_type(Path("avatar.JpG")), "image/jpeg")

    def test_no_extension_returns_octet_stream(self) -> None:
        self.assertEqual(resolve_media_type(Path("avatar")), "application/octet-stream")


# ---------------------------------------------------------------------------
# get_assets_root
# ---------------------------------------------------------------------------


class TestGetAssetsRoot(unittest.TestCase):
    """Tests for get_assets_root()."""

    def test_creates_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "assets"
            result = get_assets_root(target)
            self.assertTrue(target.exists())
            self.assertEqual(result, target)

    def test_creates_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "nested" / "assets"
            result = get_assets_root(target)
            self.assertTrue(target.exists())
            self.assertEqual(result, target)

    def test_existing_directory_is_fine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "assets"
            target.mkdir()
            result = get_assets_root(target)
            self.assertTrue(target.exists())
            self.assertEqual(result, target)


# ---------------------------------------------------------------------------
# build_avatar_dir / build_avatar_path
# ---------------------------------------------------------------------------


class TestBuildAvatarDir(unittest.TestCase):
    """Tests for build_avatar_dir()."""

    def test_returns_base_path_over_entity_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            result = build_avatar_dir(base, "entity-123")
            self.assertEqual(result, base / "entity-123")

    def test_creates_base_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            build_avatar_dir(base, "abc")
            self.assertTrue(base.exists())


class TestBuildAvatarPath(unittest.TestCase):
    """Tests for build_avatar_path()."""

    def test_with_filename_returns_full_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            result = build_avatar_path(base, "e1", "avatar.png")
            self.assertEqual(result, base / "e1" / "avatar.png")

    def test_with_none_filename_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            result = build_avatar_path(base, "e1", None)
            self.assertIsNone(result)

    def test_with_empty_string_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            result = build_avatar_path(base, "e1", "")
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# delete_avatar_files
# ---------------------------------------------------------------------------


class TestDeleteAvatarFiles(unittest.TestCase):
    """Tests for delete_avatar_files()."""

    def test_removes_directory_and_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            avatar_dir = base / "entity-1"
            avatar_dir.mkdir(parents=True)
            (avatar_dir / "avatar.png").write_bytes(b"\x89PNG")
            (avatar_dir / "extra.txt").write_bytes(b"hello")
            self.assertTrue(avatar_dir.exists())

            delete_avatar_files(base, "entity-1")
            self.assertFalse(avatar_dir.exists())

    def test_no_error_when_directory_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            # Should not raise
            delete_avatar_files(base, "nonexistent")

    def test_removes_nested_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            avatar_dir = base / "entity-2"
            avatar_dir.mkdir(parents=True)
            nested = avatar_dir / "sub"
            nested.mkdir()
            (nested / "deep.png").write_bytes(b"\x89PNG")

            delete_avatar_files(base, "entity-2")
            self.assertFalse(avatar_dir.exists())


# ---------------------------------------------------------------------------
# validate_and_save_avatar (async)
# ---------------------------------------------------------------------------


def _valid_image(content_type: str = "image/png") -> bytes:
    output = BytesIO()
    image_format = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[content_type]
    Image.new("RGB", (2, 2), color=(20, 40, 60)).save(output, format=image_format)
    return output.getvalue()


def _make_upload_file(
    content_type: str = "image/png",
    content: bytes | None = None,
) -> AsyncMock:
    """Create a mock UploadFile with the given content_type and content."""
    file = AsyncMock(spec=UploadFile)
    file.content_type = content_type
    file.read = AsyncMock(return_value=content if content is not None else _valid_image(content_type))
    return file


class TestValidateAndSaveAvatar(unittest.IsolatedAsyncioTestCase):
    """Tests for validate_and_save_avatar()."""

    async def test_valid_png_saves_and_returns_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            content = _valid_image("image/png")
            file = _make_upload_file("image/png", content)

            result = await validate_and_save_avatar(base, "user-1", file)

            self.assertEqual(result, "avatar.png")
            saved = (base / "user-1" / "avatar.png").read_bytes()
            self.assertEqual(saved, content)

    async def test_valid_jpeg_saves_and_returns_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            content = _valid_image("image/jpeg")
            file = _make_upload_file("image/jpeg", content)

            result = await validate_and_save_avatar(base, "user-2", file)

            self.assertEqual(result, "avatar.jpg")
            saved = (base / "user-2" / "avatar.jpg").read_bytes()
            self.assertEqual(saved, content)

    async def test_valid_webp_saves_and_returns_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            content = _valid_image("image/webp")
            file = _make_upload_file("image/webp", content)

            result = await validate_and_save_avatar(base, "user-3", file)

            self.assertEqual(result, "avatar.webp")

    async def test_svg_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            content = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
            file = _make_upload_file("image/svg+xml", content)

            with self.assertRaises(HTTPException):
                await validate_and_save_avatar(base, "user-4", file)

    async def test_invalid_content_type_raises_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            file = _make_upload_file("application/pdf", b"%PDF-1.4")

            with self.assertRaises(HTTPException) as ctx:
                await validate_and_save_avatar(base, "user-5", file)

            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("Unsupported avatar type", ctx.exception.detail)

    async def test_empty_file_raises_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            file = _make_upload_file("image/png", b"")

            with self.assertRaises(HTTPException) as ctx:
                await validate_and_save_avatar(base, "user-6", file)

            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("empty", ctx.exception.detail.lower())

    async def test_file_exceeding_2mb_raises_400(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            oversized = b"\x89PNG" + b"\x00" * (MAX_AVATAR_BYTES + 1)
            file = _make_upload_file("image/png", oversized)

            with self.assertRaises(HTTPException) as ctx:
                await validate_and_save_avatar(base, "user-7", file)

            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("2 MB", ctx.exception.detail)

    async def test_replaces_existing_avatar_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            avatar_dir = base / "user-8"
            avatar_dir.mkdir(parents=True)
            # Pre-populate with an old avatar
            (avatar_dir / "avatar.jpg").write_bytes(b"old-avatar-data")
            (avatar_dir / "extra.txt").write_bytes(b"extra-file")

            new_content = _valid_image("image/png")
            file = _make_upload_file("image/png", new_content)

            result = await validate_and_save_avatar(base, "user-8", file)

            self.assertEqual(result, "avatar.png")
            saved = (avatar_dir / "avatar.png").read_bytes()
            self.assertEqual(saved, new_content)
            # Old files should be gone
            self.assertFalse((avatar_dir / "avatar.jpg").exists())
            self.assertFalse((avatar_dir / "extra.txt").exists())

    async def test_exact_2mb_file_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            image = _valid_image("image/png")
            exact = image + b"\x00" * (MAX_AVATAR_BYTES - len(image))
            file = _make_upload_file("image/png", exact)

            result = await validate_and_save_avatar(base, "user-9", file)

            self.assertEqual(result, "avatar.png")


# ---------------------------------------------------------------------------
# save_avatar_bytes
# ---------------------------------------------------------------------------


class TestSaveAvatarBytes(unittest.TestCase):
    """Tests for save_avatar_bytes()."""

    def test_saves_png_bytes_and_returns_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            content = _valid_image("image/png")

            result = save_avatar_bytes(base, "agent-1", content, "image/png")

            self.assertEqual(result, "avatar.png")
            saved = (base / "agent-1" / "avatar.png").read_bytes()
            self.assertEqual(saved, content)

    def test_saves_jpeg_bytes_and_returns_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            content = _valid_image("image/jpeg")

            result = save_avatar_bytes(base, "agent-2", content, "image/jpeg")

            self.assertEqual(result, "avatar.jpg")
            saved = (base / "agent-2" / "avatar.jpg").read_bytes()
            self.assertEqual(saved, content)

    def test_default_content_type_is_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            content = _valid_image("image/png")

            result = save_avatar_bytes(base, "agent-3", content)

            self.assertEqual(result, "avatar.png")
            saved = (base / "agent-3" / "avatar.png").read_bytes()
            self.assertEqual(saved, content)

    def test_replaces_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "avatars"
            avatar_dir = base / "agent-4"
            avatar_dir.mkdir(parents=True)
            (avatar_dir / "avatar.png").write_bytes(b"old-data")
            (avatar_dir / "stale.jpg").write_bytes(b"stale")

            new_content = _valid_image("image/jpeg")
            result = save_avatar_bytes(base, "agent-4", new_content, "image/jpeg")

            self.assertEqual(result, "avatar.jpg")
            saved = (avatar_dir / "avatar.jpg").read_bytes()
            self.assertEqual(saved, new_content)
            self.assertFalse((avatar_dir / "avatar.png").exists())
            self.assertFalse((avatar_dir / "stale.jpg").exists())

    def test_creates_directory_if_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "deep" / "new" / "path"
            content = _valid_image("image/png")

            save_avatar_bytes(base, "agent-5", content)

            self.assertTrue((base / "agent-5" / "avatar.png").exists())


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


class TestConstants(unittest.TestCase):
    """Sanity checks on module-level constants."""

    def test_allowed_types_keys_and_values(self) -> None:
        self.assertEqual(
            set(ALLOWED_AVATAR_TYPES.keys()),
            {"image/png", "image/jpeg", "image/webp"},
        )
        self.assertEqual(ALLOWED_AVATAR_TYPES["image/png"], ".png")
        self.assertEqual(ALLOWED_AVATAR_TYPES["image/jpeg"], ".jpg")
        self.assertEqual(ALLOWED_AVATAR_TYPES["image/webp"], ".webp")

    def test_max_avatar_bytes_is_2mb(self) -> None:
        self.assertEqual(MAX_AVATAR_BYTES, 2 * 1024 * 1024)

    def test_avatar_media_types_has_expected_entries(self) -> None:
        self.assertIn(".png", AVATAR_MEDIA_TYPES)
        self.assertIn(".jpg", AVATAR_MEDIA_TYPES)
        self.assertIn(".jpeg", AVATAR_MEDIA_TYPES)
        self.assertIn(".webp", AVATAR_MEDIA_TYPES)
        self.assertNotIn(".svg", AVATAR_MEDIA_TYPES)


if __name__ == "__main__":
    unittest.main()
