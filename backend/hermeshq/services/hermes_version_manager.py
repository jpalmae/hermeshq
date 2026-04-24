import asyncio
import json
import re
import shutil
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.config import get_settings
from hermeshq.models.agent import Agent
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.hermes_version import HermesVersion
from hermeshq.schemas.hermes_version import (
    HermesUpstreamCatalogCreate,
    HermesUpstreamVersionRead,
    HermesVersionCreate,
    HermesVersionRead,
    HermesVersionUpdate,
)


class HermesVersionError(RuntimeError):
    pass


@dataclass
class HermesRuntimeSelection:
    requested_version: str | None
    effective_version: str
    source: str
    python_bin: str
    hermes_bin: str
    detected_version: str | None
    release_tag: str | None = None


class HermesVersionManager:
    HERMES_REPO_URL = "https://github.com/NousResearch/hermes-agent.git"
    HERMES_RAW_PYPROJECT_URL = "https://raw.githubusercontent.com/NousResearch/hermes-agent/{tag}/pyproject.toml"
    DEFAULT_CATALOG = (
        {
            "version": "0.8.0",
            "release_tag": "v2026.4.8",
            "description": "Stable rollout target prior to v0.9.0.",
        },
        {
            "version": "0.9.0",
            "release_tag": "v2026.4.13",
            "description": "Latest Hermes Agent release as of April 13, 2026.",
        },
    )
    _VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
    _TAG_RE = re.compile(r"refs/tags/(.+)$")
    _PYPROJECT_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
    _UPSTREAM_CACHE_TTL_SECONDS = 900

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.settings = get_settings()
        self.root = (self.settings.workspaces_root / "_hermes_versions").resolve()
        self._upstream_cache: tuple[float, list[HermesUpstreamVersionRead]] | None = None

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    async def ensure_default_catalog_entries(self) -> None:
        async with self.session_factory() as session:
            for item in self.DEFAULT_CATALOG:
                record = await session.get(HermesVersion, item["version"])
                if not record:
                    session.add(HermesVersion(**item))
            await session.commit()

    async def list_catalog_entries(self) -> list[HermesVersion]:
        async with self.session_factory() as session:
            result = await session.execute(select(HermesVersion))
            return sorted(result.scalars().all(), key=lambda item: self._version_sort_key(item.version), reverse=True)

    async def get_catalog_entry(self, version: str) -> HermesVersion:
        async with self.session_factory() as session:
            record = await session.get(HermesVersion, version)
            if not record:
                raise HermesVersionError(f"Hermes version '{version}' is not in the catalog")
            return record

    async def create_catalog_entry(self, payload: HermesVersionCreate) -> HermesVersionRead:
        version = payload.version.strip()
        if not version:
            raise HermesVersionError("Version is required")
        release_tag = (payload.release_tag or "").strip() or None
        if release_tag:
            await self.ensure_release_tag_exists(release_tag)
        async with self.session_factory() as session:
            existing = await session.get(HermesVersion, version)
            if existing:
                raise HermesVersionError(f"Hermes version '{version}' already exists in the catalog")
            session.add(
                HermesVersion(
                    version=version,
                    release_tag=release_tag,
                    description=(payload.description or "").strip() or None,
                )
            )
            await session.commit()
        default_version = await self.get_default_version()
        return await self.describe_version(version, default_version)

    async def update_catalog_entry(self, version: str, payload: HermesVersionUpdate) -> HermesVersionRead:
        release_tag = (payload.release_tag or "").strip() or None
        if release_tag:
            await self.ensure_release_tag_exists(release_tag)
        async with self.session_factory() as session:
            record = await session.get(HermesVersion, version)
            if not record:
                raise HermesVersionError(f"Hermes version '{version}' is not in the catalog")
            record.release_tag = release_tag
            record.description = (payload.description or "").strip() or None
            await session.commit()
        default_version = await self.get_default_version()
        return await self.describe_version(version, default_version)

    async def create_catalog_entry_from_upstream(self, payload: HermesUpstreamCatalogCreate) -> HermesVersionRead:
        release = await self.get_upstream_release(payload.release_tag)
        async with self.session_factory() as session:
            existing_by_tag = await session.execute(
                select(HermesVersion).where(HermesVersion.release_tag == release.release_tag)
            )
            if existing_by_tag.scalars().first():
                raise HermesVersionError(f"Hermes release tag '{release.release_tag}' already exists in the catalog")

            preferred_version = self._preferred_catalog_version_for_release(release)
            version = await self._next_available_catalog_version(session, preferred_version, release.release_tag)

            session.add(
                HermesVersion(
                    version=version,
                    release_tag=release.release_tag,
                    description=(payload.description or "").strip() or None,
                )
            )
            await session.commit()
        default_version = await self.get_default_version()
        return await self.describe_version(version, default_version)

    async def delete_catalog_entry(self, version: str) -> None:
        if self.is_installed(version):
            raise HermesVersionError("Cannot remove a catalog entry while the version is installed")
        default_version = await self.get_default_version()
        if default_version == version:
            raise HermesVersionError("Cannot remove the current default Hermes version from the catalog")
        if await self.count_pinned_agents(version):
            raise HermesVersionError("Cannot remove a catalog entry that is still pinned by agents")
        async with self.session_factory() as session:
            record = await session.get(HermesVersion, version)
            if not record:
                raise HermesVersionError(f"Hermes version '{version}' is not in the catalog")
            await session.delete(record)
            await session.commit()

    def version_root(self, version: str) -> Path:
        return self.ensure_root() / version

    def python_path(self, version: str) -> Path:
        return self.version_root(version) / ".venv" / "bin" / "python"

    def hermes_path(self, version: str) -> Path:
        return self.version_root(version) / ".venv" / "bin" / "hermes"

    def metadata_path(self, version: str) -> Path:
        return self.version_root(version) / "metadata.json"

    def is_installed(self, version: str) -> bool:
        return self.python_path(version).exists() and self.hermes_path(version).exists()

    def _read_metadata(self, version: str) -> dict:
        path = self.metadata_path(version)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_metadata(self, version: str, payload: dict) -> None:
        self.version_root(version).mkdir(parents=True, exist_ok=True)
        self.metadata_path(version).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _extract_version(self, text: str) -> str | None:
        match = self._VERSION_RE.search(text or "")
        return match.group(1) if match else None

    def _version_sort_key(self, value: str) -> tuple[int, ...]:
        match = self._VERSION_RE.search(value or "")
        if not match:
            return (0,)
        return tuple(int(part) for part in match.group(1).split("."))

    async def _run_capture(self, *command: str) -> tuple[int, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            return 127, str(exc)
        stdout, _ = await process.communicate()
        return process.returncode, stdout.decode("utf-8", errors="replace").strip()

    async def detect_bundled_version(self) -> str | None:
        hermes_bin = shutil.which("hermes")
        if hermes_bin:
            code, output = await self._run_capture(hermes_bin, "--version")
            if code == 0:
                detected = self._extract_version(output)
                if detected:
                    return detected
        code, output = await self._run_capture(
            sys.executable,
            "-c",
            "import importlib.metadata; print(importlib.metadata.version('hermes-agent'))",
        )
        if code == 0:
            return self._extract_version(output)
        return None

    async def detect_installed_version(self, version: str) -> str | None:
        hermes_bin = self.hermes_path(version)
        if not hermes_bin.exists():
            return None
        code, output = await self._run_capture(str(hermes_bin), "--version")
        if code == 0:
            return self._extract_version(output)
        return None

    async def list_upstream_releases(self, *, force_refresh: bool = False) -> list[HermesUpstreamVersionRead]:
        now = time.time()
        if (
            not force_refresh
            and self._upstream_cache is not None
            and now - self._upstream_cache[0] < self._UPSTREAM_CACHE_TTL_SECONDS
        ):
            return self._upstream_cache[1]

        code, output = await self._run_capture("git", "ls-remote", "--tags", "--refs", self.HERMES_REPO_URL)
        if code != 0:
            raise HermesVersionError(output or "Failed to fetch upstream Hermes tags")

        tags: list[tuple[str, str]] = []
        for line in output.splitlines():
            parts = line.strip().split("\t")
            if len(parts) != 2:
                continue
            commit_sha, ref = parts
            match = self._TAG_RE.match(ref)
            if not match:
                continue
            tags.append((match.group(1), commit_sha))
        tags.sort(key=lambda item: self._version_sort_key(item[0].removeprefix("v")), reverse=True)

        async with self.session_factory() as session:
            catalog_entries = (await session.execute(select(HermesVersion))).scalars().all()
        versions_by_tag: dict[str, list[str]] = {}
        for entry in catalog_entries:
            if entry.release_tag:
                versions_by_tag.setdefault(entry.release_tag, []).append(entry.version)

        releases: list[HermesUpstreamVersionRead] = []
        for tag, commit_sha in tags:
            detected_version = await self._fetch_upstream_package_version(tag)
            catalog_versions = sorted(versions_by_tag.get(tag, []), key=self._version_sort_key, reverse=True)
            releases.append(
                HermesUpstreamVersionRead(
                    release_tag=tag,
                    commit_sha=commit_sha,
                    detected_version=detected_version,
                    catalog_versions=catalog_versions,
                    already_in_catalog=bool(catalog_versions),
                )
            )

        self._upstream_cache = (now, releases)
        return releases

    async def get_upstream_release(self, release_tag: str) -> HermesUpstreamVersionRead:
        normalized = release_tag.strip()
        if not normalized:
            raise HermesVersionError("Release tag is required")
        for release in await self.list_upstream_releases():
            if release.release_tag == normalized:
                return release
        raise HermesVersionError(f"Hermes release tag '{normalized}' was not found in upstream")

    async def ensure_release_tag_exists(self, release_tag: str) -> None:
        await self.get_upstream_release(release_tag)

    async def install_version(self, version: str) -> HermesVersionRead:
        catalog = await self.get_catalog_entry(version)
        if not catalog.release_tag:
            raise HermesVersionError("Configure a release tag first before installing this Hermes version")
        await self.ensure_release_tag_exists(catalog.release_tag)
        target_root = self.version_root(version)
        if target_root.exists() and self.is_installed(version):
            detected = await self.detect_installed_version(version)
            if detected:
                return await self.describe_version(version, default_version=None)
        if target_root.exists():
            shutil.rmtree(target_root)
        target_root.mkdir(parents=True, exist_ok=True)
        try:
            code, output = await self._run_capture(sys.executable, "-m", "venv", str(target_root / ".venv"))
            if code != 0:
                raise HermesVersionError(output or f"Failed to create venv for Hermes {version}")

            pip_bin = target_root / ".venv" / "bin" / "pip"
            install_commands = [
                (str(pip_bin), "install", "--upgrade", "pip", "wheel", "setuptools"),
                (
                    str(pip_bin),
                    "install",
                    "--no-cache-dir",
                    f"git+{self.HERMES_REPO_URL}@{catalog.release_tag}",
                ),
            ]
            for command in install_commands:
                code, output = await self._run_capture(*command)
                if code != 0:
                    raise HermesVersionError(output or f"Failed to install Hermes {version}")
            detected_version = await self.detect_installed_version(version)
            self._write_metadata(
                version,
                {
                    "version": version,
                    "release_tag": catalog.release_tag,
                    "detected_version": detected_version,
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            if target_root.exists():
                shutil.rmtree(target_root)
            raise

        return await self.describe_version(version, default_version=None)

    async def uninstall_version(self, version: str) -> None:
        if not self.is_installed(version):
            return
        target_root = self.version_root(version)
        shutil.rmtree(target_root)

    async def get_default_version(self) -> str | None:
        async with self.session_factory() as session:
            settings_row = await session.get(AppSettings, "default")
            return settings_row.default_hermes_version if settings_row else None

    async def set_default_version(self, version: str | None) -> None:
        async with self.session_factory() as session:
            settings_row = await session.get(AppSettings, "default")
            if not settings_row:
                settings_row = AppSettings(id="default")
                session.add(settings_row)
            settings_row.default_hermes_version = version
            await session.commit()

    async def count_pinned_agents(self, version: str) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(Agent).where(Agent.hermes_version == version)
            )
            return int(result.scalar_one() or 0)

    async def describe_version(self, version: str, default_version: str | None) -> HermesVersionRead:
        catalog = await self.get_catalog_entry(version)
        installed = self.is_installed(version)
        metadata = self._read_metadata(version)
        detected_version = metadata.get("detected_version")
        if installed and not detected_version:
            detected_version = await self.detect_installed_version(version)
        version_matches_detected = None
        detected_version_warning = None
        if detected_version:
            version_matches_detected = version == detected_version
            if not version_matches_detected:
                detected_version_warning = (
                    f"Catalog version '{version}' differs from detected Hermes runtime version '{detected_version}'."
                )
        return HermesVersionRead(
            version=version,
            release_tag=catalog.release_tag,
            description=catalog.description,
            source="managed",
            installed=installed,
            install_status="ready" if installed else "available",
            installed_path=str(self.version_root(version)) if installed else None,
            detected_version=detected_version,
            version_matches_detected=version_matches_detected,
            detected_version_warning=detected_version_warning,
            is_default=default_version == version,
            is_effective_default=default_version == version,
            in_use_by_agents=await self.count_pinned_agents(version),
        )

    async def list_versions(self) -> list[HermesVersionRead]:
        default_version = await self.get_default_version()
        bundled_version = await self.detect_bundled_version()
        versions = [
            HermesVersionRead(
                version="bundled",
                release_tag=None,
                description="Backend bundled Hermes runtime.",
                source="bundled",
                installed=True,
                install_status="ready",
                installed_path=shutil.which("hermes"),
                detected_version=bundled_version,
                version_matches_detected=True if bundled_version else None,
                detected_version_warning=None,
                is_default=default_version is None,
                is_effective_default=default_version is None,
                in_use_by_agents=0,
            )
        ]
        for catalog in await self.list_catalog_entries():
            versions.append(await self.describe_version(catalog.version, default_version))
        return versions

    async def resolve_runtime(self, requested_version: str | None) -> HermesRuntimeSelection:
        if requested_version:
            catalog = await self.get_catalog_entry(requested_version)
            if not self.is_installed(requested_version):
                raise HermesVersionError(f"Hermes version '{requested_version}' is not installed")
            detected_version = self._read_metadata(requested_version).get("detected_version")
            if not detected_version:
                detected_version = await self.detect_installed_version(requested_version)
            return HermesRuntimeSelection(
                requested_version=requested_version,
                effective_version=requested_version,
                source="managed",
                python_bin=str(self.python_path(requested_version)),
                hermes_bin=str(self.hermes_path(requested_version)),
                detected_version=detected_version,
                release_tag=catalog.release_tag,
            )

        hermes_bin = shutil.which("hermes") or "hermes"
        bundled_version = await self.detect_bundled_version()
        return HermesRuntimeSelection(
            requested_version=None,
            effective_version=bundled_version or "bundled",
            source="bundled",
            python_bin=sys.executable,
            hermes_bin=hermes_bin,
            detected_version=bundled_version,
            release_tag=None,
        )

    async def _fetch_upstream_package_version(self, tag: str) -> str | None:
        def _read_pyproject() -> str:
            with urllib.request.urlopen(
                self.HERMES_RAW_PYPROJECT_URL.format(tag=tag),
                timeout=15,
            ) as response:
                return response.read().decode("utf-8", errors="replace")

        try:
            text = await asyncio.to_thread(_read_pyproject)
        except Exception:
            return None
        match = self._PYPROJECT_VERSION_RE.search(text)
        if not match:
            return None
        return match.group(1).strip() or None

    def _preferred_catalog_version_for_release(self, release: HermesUpstreamVersionRead) -> str:
        if release.detected_version:
            return release.detected_version
        return release.release_tag.removeprefix("v")

    async def _next_available_catalog_version(
        self,
        session: AsyncSession,
        preferred_version: str,
        release_tag: str,
    ) -> str:
        candidate = preferred_version.strip()
        if not candidate:
            candidate = release_tag.removeprefix("v")
        existing = await session.get(HermesVersion, candidate)
        if not existing:
            return candidate
        suffix_base = f"{candidate}@{release_tag.removeprefix('v')}"
        existing = await session.get(HermesVersion, suffix_base)
        if not existing:
            return suffix_base[:32]
        counter = 2
        while True:
            numbered = f"{suffix_base}-{counter}"[:32]
            existing = await session.get(HermesVersion, numbered)
            if not existing:
                return numbered
            counter += 1
