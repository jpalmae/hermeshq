from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from hermeshq.models.agent import Agent
from hermeshq.services.hermes_installation import HermesInstallationManager
from hermeshq.services.hermes_installation.manager import HermesInstallationError

logger = logging.getLogger(__name__)

MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024
MAX_FILE_COUNT = 50
ALLOWED_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".py",
        ".sh",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".csv",
        ".html",
        ".css",
        ".js",
        ".ts",
        ".sql",
        ".cfg",
        ".ini",
        ".example",
        ".gitignore",
    }
)
BINARY_SNIFF_BYTES = 512


class SkillTransferError(ValueError):
    def __init__(self, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass
class SkillTransferResult:
    name: str
    description: str
    target_path: str
    files: int
    bytes: int


def _validate_name(name: str) -> str:
    cleaned = (name or "").strip().strip("/")
    if not cleaned or cleaned in (".", ".."):
        raise SkillTransferError("Invalid skill name")
    if any(part in ("", ".", "..") for part in cleaned.split("/")):
        raise SkillTransferError("Invalid skill name")
    if len(cleaned) > 128 or any(len(part) > 64 for part in cleaned.split("/")):
        raise SkillTransferError("Skill name too long")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/.")
    if not set(cleaned) <= allowed:
        raise SkillTransferError("Skill name may only contain letters, digits, dash, underscore, slash and dot")
    return cleaned


def _is_binary(path: Path) -> bool:
    try:
        head = path.read_bytes()[:BINARY_SNIFF_BYTES]
    except OSError:
        return True
    return b"\x00" in head


def _collect_files(source_dir: Path) -> list[Path]:
    files: list[Path] = []
    for item in sorted(source_dir.rglob("*")):
        if item.is_symlink():
            raise SkillTransferError(f"Symlinks are not allowed in skills: {item.name}")
        if item.is_dir():
            continue
        files.append(item)
    if not files:
        raise SkillTransferError("Skill directory is empty")
    if len(files) > MAX_FILE_COUNT:
        raise SkillTransferError(f"Skill has too many files (max {MAX_FILE_COUNT})", status_code=413)
    total = 0
    for file_path in files:
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            raise SkillTransferError(f"Cannot read skill file: {file_path.name}") from exc
        if size > MAX_FILE_BYTES:
            raise SkillTransferError(f"File too large: {file_path.name} (max 512KB)", status_code=413)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise SkillTransferError("Skill exceeds 5MB total", status_code=413)
        if file_path.suffix.lower() not in ALLOWED_EXTENSIONS and file_path.name != ".hermeshq-skill.json":
            raise SkillTransferError(f"File type not allowed: {file_path.name}")
        if _is_binary(file_path) and file_path.name != ".hermeshq-skill.json":
            raise SkillTransferError(f"Binary files are not allowed: {file_path.name}")
    return files


def _extract_description(skill_md: Path) -> str:
    try:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:500]
    return ""


class SkillTransferService:
    def __init__(self, installation_manager: HermesInstallationManager) -> None:
        self._installation_manager = installation_manager

    def _skills_root(self, agent: Agent) -> Path:
        hermes_home = self._installation_manager.build_hermes_home(agent.workspace_path)
        return (hermes_home / "skills").resolve()

    async def list_skills(self, agent: Agent) -> list[dict]:
        return await self._installation_manager.list_installed_skills(agent)

    async def transfer(
        self,
        *,
        source_agent: Agent,
        target_agent: Agent,
        skill_path: str,
        target_name: str | None = None,
        overwrite: bool = False,
    ) -> SkillTransferResult:
        if source_agent.id == target_agent.id:
            raise SkillTransferError("Source and target agents must differ")
        if target_agent.is_archived:
            raise SkillTransferError("Target agent is archived")

        source_root = self._skills_root(source_agent)
        source_dir = (source_root / (skill_path or "").strip().strip("/")).resolve()
        try:
            source_dir.relative_to(source_root)
        except ValueError as exc:
            raise SkillTransferError("Invalid skill path") from exc
        if not source_dir.is_dir() or not (source_dir / "SKILL.md").is_file():
            raise SkillTransferError("Skill not found in source agent", status_code=404)

        resolved_name = _validate_name(target_name or source_dir.name)
        target_root = self._skills_root(target_agent)
        target_dir = (target_root / resolved_name).resolve()
        try:
            target_dir.relative_to(target_root)
        except ValueError as exc:
            raise SkillTransferError("Invalid target skill name") from exc
        if target_dir.exists():
            if not overwrite:
                raise SkillTransferError(
                    f"Skill '{resolved_name}' already exists in target agent (use overwrite)",
                    status_code=409,
                )
            shutil.rmtree(target_dir)

        files = _collect_files(source_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        total_bytes = 0
        try:
            for file_path in files:
                relative = file_path.relative_to(source_dir)
                if file_path.name == ".hermeshq-skill.json":
                    continue
                destination = target_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file_path, destination)
                copied += 1
                total_bytes += file_path.stat().st_size
        except OSError as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise SkillTransferError("Failed to copy skill files") from exc

        description = _extract_description(target_dir / "SKILL.md")
        logger.info(
            "Skill transferred: %s from agent %s to agent %s (%d files, %d bytes)",
            resolved_name,
            source_agent.id,
            target_agent.id,
            copied,
            total_bytes,
        )
        return SkillTransferResult(
            name=resolved_name,
            description=description,
            target_path=resolved_name,
            files=copied,
            bytes=total_bytes,
        )


def raise_installation_error_passthrough(exc: HermesInstallationError) -> None:
    raise exc
