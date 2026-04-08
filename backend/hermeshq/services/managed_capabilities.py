from __future__ import annotations

import shutil
import tarfile
import tempfile
from pathlib import Path

import yaml

from hermeshq.config import get_settings

CORE_MANAGED_PLUGIN_CATALOG: list[dict] = [
    {
        "slug": "hermeshq_comms",
        "template_dir": "hermeshq_comms",
        "toolset": "hermeshq_comms",
        "standard_compatible": True,
    },
]


def plugin_templates_root() -> Path:
    return Path(__file__).resolve().parents[1] / "plugin_templates"


def skill_templates_root() -> Path:
    return Path(__file__).resolve().parents[1] / "skill_templates"


def bundled_integration_packages_root() -> Path:
    return Path(__file__).resolve().parents[1] / "integration_packages"


def uploaded_integration_packages_root() -> Path:
    root = get_settings().integration_packages_root
    root.mkdir(parents=True, exist_ok=True)
    return root


def list_managed_plugins(enabled_integration_slugs: list[str] | None = None) -> list[dict]:
    plugins = [dict(item) for item in CORE_MANAGED_PLUGIN_CATALOG]
    for integration in list_managed_integrations(enabled_integration_slugs):
        plugin_dir = integration.get("plugin_source_root")
        plugin_slug = integration.get("plugin_slug")
        if not plugin_dir or not plugin_slug:
            continue
        plugins.append(
            {
                "slug": plugin_slug,
                "template_dir": plugin_slug,
                "toolset": plugin_slug,
                "standard_compatible": bool(integration.get("standard_compatible")),
                "source_root": plugin_dir,
            }
        )
    return plugins


def list_standard_compatible_toolsets(enabled_integration_slugs: list[str] | None = None) -> list[str]:
    return [
        item["toolset"]
        for item in list_managed_plugins(enabled_integration_slugs)
        if item.get("standard_compatible")
    ]


def list_available_integration_packages(enabled_integration_slugs: list[str] | None = None) -> list[dict]:
    enabled = set(enabled_integration_slugs or [])
    packages: list[dict] = []
    for root, source_type in (
        (bundled_integration_packages_root(), "bundled"),
        (uploaded_integration_packages_root(), "uploaded"),
    ):
        if not root.exists():
            continue
        for manifest_path in sorted(root.glob("*/manifest.yaml")):
            package_root = manifest_path.parent
            manifest = _read_yaml(manifest_path)
            if not isinstance(manifest, dict):
                continue
            slug = str(manifest.get("slug") or package_root.name).strip()
            if not slug:
                continue
            plugin_source_root = package_root / str(manifest.get("plugin_dir") or "plugin")
            plugin_meta = _read_yaml(plugin_source_root / "plugin.yaml") if plugin_source_root.exists() else {}
            packages.append(
                {
                    "slug": slug,
                    "name": str(manifest.get("name") or slug),
                    "description": str(manifest.get("description") or ""),
                    "version": str(manifest.get("version") or "1.0.0"),
                    "source_type": source_type,
                    "installed": slug in enabled,
                    "standard_compatible": bool(manifest.get("standard_compatible", False)),
                    "supported_profiles": list(manifest.get("supported_profiles") or ["standard", "technical", "security"]),
                    "required_fields": list(manifest.get("required_fields") or []),
                    "fields": list(manifest.get("fields") or []),
                    "defaults": dict(manifest.get("defaults") or {}),
                    "secret_provider": manifest.get("secret_provider"),
                    "plugin_slug": manifest.get("plugin_slug"),
                    "plugin_name": plugin_meta.get("name"),
                    "plugin_description": plugin_meta.get("description") or manifest.get("description"),
                    "plugin_source_root": plugin_source_root if plugin_source_root.exists() else None,
                    "skill_identifier": manifest.get("skill_identifier"),
                    "skill_source_root": _skill_source_root(package_root, manifest),
                    "test_action": manifest.get("test_action"),
                    "healthcheck_path": manifest.get("healthcheck_path") or "healthcheck.py",
                    "env_map": dict(manifest.get("env_map") or {}),
                    "tools": list(plugin_meta.get("provides_tools") or []),
                    "package_root": package_root,
                }
            )
    return packages


def list_managed_integrations(enabled_integration_slugs: list[str] | None = None) -> list[dict]:
    enabled = set(enabled_integration_slugs or [])
    return [
        _public_package_record(item)
        for item in list_available_integration_packages(enabled_integration_slugs)
        if item["slug"] in enabled
    ]


def get_managed_integration(
    slug: str,
    enabled_integration_slugs: list[str] | None = None,
    *,
    include_uninstalled: bool = False,
) -> dict | None:
    enabled = set(enabled_integration_slugs or [])
    for item in list_available_integration_packages(enabled_integration_slugs):
        if item["slug"] != slug:
            continue
        if include_uninstalled or item["slug"] in enabled:
            return _public_package_record(item)
    return None


def list_local_skill_templates(
    query: str = "",
    limit: int = 20,
    enabled_integration_slugs: list[str] | None = None,
) -> list[dict]:
    normalized_query = query.strip().lower()
    templates: list[dict] = []
    for package in list_managed_integrations(enabled_integration_slugs):
        skill_root = package.get("skill_source_root")
        if not skill_root:
            continue
        skill_md = Path(skill_root) / "SKILL.md"
        if not skill_md.exists():
            continue
        content = skill_md.read_text(encoding="utf-8")
        description = _extract_description(content)
        haystack = f"{package['slug']}\n{description}\n{content}".lower()
        if normalized_query and normalized_query not in haystack:
            continue
        templates.append(
            {
                "name": Path(skill_root).name,
                "description": description,
                "identifier": package.get("skill_identifier") or f"local/{Path(skill_root).name}",
                "source": "hermeshq-local",
                "trust_level": "internal",
                "repo": "hermeshq",
                "path": str(skill_root),
                "tags": ["local", "integration"],
                "extra": {"managed_by": "HermesHQ", "integration_slug": package["slug"]},
            }
        )
        if len(templates) >= limit:
            break

    root = skill_templates_root()
    if root.exists():
        for skill_md in sorted(root.glob("*/SKILL.md")):
            skill_dir = skill_md.parent
            slug = skill_dir.name
            content = skill_md.read_text(encoding="utf-8")
            description = _extract_description(content)
            haystack = f"{slug}\n{description}\n{content}".lower()
            if normalized_query and normalized_query not in haystack:
                continue
            templates.append(
                {
                    "name": slug,
                    "description": description,
                    "identifier": f"local/{slug}",
                    "source": "hermeshq-local",
                    "trust_level": "internal",
                    "repo": "hermeshq",
                    "path": str(skill_dir),
                    "tags": ["local"],
                    "extra": {"managed_by": "HermesHQ"},
                }
            )
            if len(templates) >= limit:
                break
    return templates[:limit]


def fetch_local_skill_bundle(identifier: str, enabled_integration_slugs: list[str] | None = None) -> dict | None:
    normalized = identifier.removeprefix("local/").strip()
    for package in list_managed_integrations(enabled_integration_slugs):
        skill_identifier = str(package.get("skill_identifier") or "").removeprefix("local/").strip()
        skill_root = package.get("skill_source_root")
        if skill_identifier == normalized and skill_root:
            return _skill_bundle_from_dir(Path(skill_root), package.get("skill_identifier") or identifier)

    skill_dir = skill_templates_root() / normalized
    if skill_dir.exists():
        return _skill_bundle_from_dir(skill_dir, f"local/{normalized}")
    return None


def install_uploaded_integration_package(file_path: Path) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_root = Path(tmpdir)
        with tarfile.open(file_path, "r:*") as archive:
            _safe_extract_tar(archive, tmp_root)
        package_root = _locate_package_root(tmp_root)
        manifest = _read_yaml(package_root / "manifest.yaml")
        if not isinstance(manifest, dict):
            raise ValueError("Integration package manifest is invalid")
        slug = str(manifest.get("slug") or package_root.name).strip()
        if not slug:
            raise ValueError("Integration package slug is missing")
        destination = uploaded_integration_packages_root() / slug
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(package_root, destination)
    installed = next((item for item in list_available_integration_packages([]) if item["slug"] == slug), None)
    if not installed:
        raise ValueError("Integration package could not be loaded after upload")
    return _public_package_record(installed)


def uninstall_uploaded_integration_package(slug: str) -> None:
    path = uploaded_integration_packages_root() / slug
    if path.exists():
        shutil.rmtree(path)


def list_known_integration_toolsets(enabled_integration_slugs: list[str] | None = None) -> list[str]:
    known = []
    for package in list_available_integration_packages(enabled_integration_slugs):
        plugin_slug = package.get("plugin_slug")
        if plugin_slug:
            known.append(str(plugin_slug))
    return known


def _public_package_record(item: dict) -> dict:
    return {
        "slug": item["slug"],
        "name": item["name"],
        "description": item["description"],
        "version": item["version"],
        "source_type": item["source_type"],
        "installed": item["installed"],
        "standard_compatible": item["standard_compatible"],
        "supported_profiles": item["supported_profiles"],
        "required_fields": item["required_fields"],
        "fields": item["fields"],
        "defaults": item["defaults"],
        "secret_provider": item.get("secret_provider"),
        "plugin_slug": item.get("plugin_slug"),
        "plugin_name": item.get("plugin_name"),
        "plugin_description": item.get("plugin_description"),
        "plugin_source_root": item.get("plugin_source_root"),
        "skill_identifier": item.get("skill_identifier"),
        "skill_source_root": item.get("skill_source_root"),
        "test_action": item.get("test_action"),
        "healthcheck_path": item.get("healthcheck_path"),
        "env_map": item["env_map"],
        "tools": item["tools"],
        "package_root": item.get("package_root"),
    }


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _extract_description(skill_md: str) -> str:
    for line in skill_md.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("description:"):
            return stripped.split(":", 1)[1].strip().strip("\"'")
    for line in skill_md.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:200]
    return ""


def _skill_source_root(package_root: Path, manifest: dict) -> Path | None:
    skill_dir = manifest.get("skill_dir")
    if not skill_dir:
        return None
    root = package_root / str(skill_dir)
    return root if root.exists() else None


def _skill_bundle_from_dir(skill_dir: Path, identifier: str) -> dict | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    files: dict[str, str] = {}
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(skill_dir))] = path.read_text(encoding="utf-8")
    return {
        "name": skill_dir.name,
        "files": files,
        "source": "hermeshq-local",
        "identifier": identifier,
        "trust_level": "internal",
        "metadata": {"managed_by": "HermesHQ"},
    }


def _safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
    for member in archive.getmembers():
        target = (destination / member.name).resolve()
        if destination.resolve() not in target.parents and target != destination.resolve():
            raise ValueError("Integration package archive contains unsafe paths")
    archive.extractall(destination)


def _locate_package_root(tmp_root: Path) -> Path:
    direct = tmp_root / "manifest.yaml"
    if direct.exists():
        return tmp_root
    manifests = list(tmp_root.glob("*/manifest.yaml"))
    if len(manifests) == 1:
        return manifests[0].parent
    raise ValueError("Integration package must contain a manifest.yaml at the root")
