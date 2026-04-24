from __future__ import annotations

import py_compile
import re
import shutil
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from hermeshq.config import get_settings
from hermeshq.models.integration_draft import IntegrationDraft
from hermeshq.schemas.integration_factory import (
    IntegrationDraftCheckRead,
    IntegrationDraftCreate,
    IntegrationDraftFileContentRead,
    IntegrationDraftFileRead,
    IntegrationDraftRead,
    IntegrationDraftUpdate,
    IntegrationDraftValidationRead,
)
from hermeshq.services.managed_capabilities import install_uploaded_integration_package

SLUG_PATTERN = re.compile(r"[^a-z0-9-]+")
MAX_DRAFT_FILE_BYTES = 512 * 1024


def integration_factory_root() -> Path:
    root = get_settings().workspaces_root / "_integration_factory"
    root.mkdir(parents=True, exist_ok=True)
    return root


def integration_factory_drafts_root() -> Path:
    root = integration_factory_root() / "drafts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def normalize_draft_slug(value: str) -> str:
    normalized = SLUG_PATTERN.sub("-", value.strip().lower()).strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    if not normalized:
        raise ValueError("Draft slug is invalid")
    return normalized[:128]


def draft_package_root(draft: IntegrationDraft) -> Path:
    return integration_factory_drafts_root() / draft.slug


def create_draft_files(draft: IntegrationDraft, payload: IntegrationDraftCreate) -> None:
    root = draft_package_root(draft)
    if root.exists():
        raise ValueError(f"Integration draft '{draft.slug}' already exists on disk")
    root.mkdir(parents=True, exist_ok=False)

    package_slug = draft.slug
    plugin_slug = f"hermeshq_{package_slug.replace('-', '_')}"
    tool_slug = f"{package_slug.replace('-', '_')}_ping"
    env_base = package_slug.replace("-", "_").upper()

    files = _empty_template_files(
        package_slug=package_slug,
        plugin_slug=plugin_slug,
        name=payload.name,
        description=payload.description,
        version=payload.version,
    )
    if payload.template == "rest-api":
        files = _rest_api_template_files(
            package_slug=package_slug,
            plugin_slug=plugin_slug,
            tool_slug=tool_slug,
            name=payload.name,
            description=payload.description,
            version=payload.version,
            env_base=env_base,
        )

    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def delete_draft_files(draft: IntegrationDraft) -> None:
    root = draft_package_root(draft)
    if root.exists():
        shutil.rmtree(root)


def build_draft_read(draft: IntegrationDraft) -> IntegrationDraftRead:
    root = draft_package_root(draft)
    manifest = _read_yaml(root / "manifest.yaml")
    files = _list_files(root)
    return IntegrationDraftRead(
        id=draft.id,
        slug=draft.slug,
        name=str(manifest.get("name") or draft.slug),
        description=str(manifest.get("description") or ""),
        version=str(manifest.get("version") or "0.1.0"),
        template=draft.template,
        status=draft.status,
        created_by_user_id=draft.created_by_user_id,
        created_by_agent_id=draft.created_by_agent_id,
        plugin_slug=str(manifest.get("plugin_slug") or "") or None,
        skill_identifier=str(manifest.get("skill_identifier") or "") or None,
        standard_compatible=bool(manifest.get("standard_compatible", True)),
        supported_profiles=[str(item) for item in (manifest.get("supported_profiles") or []) if str(item).strip()],
        files=files,
        last_validation=_validation_from_record(draft.last_validation),
        published_package_slug=draft.published_package_slug,
        published_package_version=draft.published_package_version,
        published_at=draft.published_at.isoformat() if draft.published_at else None,
        notes=draft.notes,
    )


def read_draft_file(draft: IntegrationDraft, relative_path: str) -> IntegrationDraftFileContentRead:
    target = _resolve_draft_file(draft, relative_path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(relative_path)
    if target.stat().st_size > MAX_DRAFT_FILE_BYTES:
        raise ValueError("Draft file is too large to open in the editor")
    return IntegrationDraftFileContentRead(path=str(relative_path), content=target.read_text(encoding="utf-8"))


def write_draft_file(draft: IntegrationDraft, relative_path: str, content: str) -> None:
    target = _resolve_draft_file(draft, relative_path, create_parent=True)
    target.write_text(content, encoding="utf-8")


def remove_draft_file(draft: IntegrationDraft, relative_path: str) -> None:
    target = _resolve_draft_file(draft, relative_path)
    if not target.exists():
        return
    target.unlink()


def update_draft_metadata(draft: IntegrationDraft, payload: IntegrationDraftUpdate) -> None:
    root = draft_package_root(draft)
    manifest_path = root / "manifest.yaml"
    manifest = _read_yaml(manifest_path)
    if not manifest:
        raise ValueError("Draft manifest.yaml is missing or invalid")
    if payload.name is not None:
        manifest["name"] = payload.name.strip()
    if payload.description is not None:
        manifest["description"] = payload.description
    if payload.version is not None:
        manifest["version"] = payload.version.strip()
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=False), encoding="utf-8")
    if payload.notes is not None:
        draft.notes = payload.notes


def validate_draft(draft: IntegrationDraft) -> IntegrationDraftValidationRead:
    root = draft_package_root(draft)
    checks: list[IntegrationDraftCheckRead] = []
    manifest_path = root / "manifest.yaml"
    plugin_init_path = root / "plugin" / "__init__.py"
    plugin_yaml_path = root / "plugin" / "plugin.yaml"

    if not root.exists():
        checks.append(IntegrationDraftCheckRead(level="error", code="draft_missing", message="Draft package root is missing."))
        return _validation_result(False, checks)

    manifest = _read_yaml(manifest_path)
    if not manifest:
        checks.append(IntegrationDraftCheckRead(level="error", code="manifest_missing", message="manifest.yaml is missing or invalid.", path="manifest.yaml"))
    else:
        if str(manifest.get("slug") or "").strip() != draft.slug:
            checks.append(IntegrationDraftCheckRead(level="error", code="manifest_slug_mismatch", message="manifest.yaml slug must match the draft slug.", path="manifest.yaml"))
        if not str(manifest.get("name") or "").strip():
            checks.append(IntegrationDraftCheckRead(level="error", code="manifest_name_missing", message="manifest.yaml is missing 'name'.", path="manifest.yaml"))
        if not str(manifest.get("plugin_slug") or "").strip():
            checks.append(IntegrationDraftCheckRead(level="error", code="manifest_plugin_slug_missing", message="manifest.yaml is missing 'plugin_slug'.", path="manifest.yaml"))
        if not manifest.get("supported_profiles"):
            checks.append(IntegrationDraftCheckRead(level="warning", code="manifest_supported_profiles_missing", message="manifest.yaml does not declare supported_profiles.", path="manifest.yaml"))

    plugin_yaml = _read_yaml(plugin_yaml_path)
    if not plugin_yaml:
        checks.append(IntegrationDraftCheckRead(level="error", code="plugin_yaml_missing", message="plugin/plugin.yaml is missing or invalid.", path="plugin/plugin.yaml"))
    elif not isinstance(plugin_yaml.get("provides_tools") or [], list):
        checks.append(IntegrationDraftCheckRead(level="error", code="plugin_tools_invalid", message="plugin/plugin.yaml provides_tools must be a list.", path="plugin/plugin.yaml"))
    elif not (plugin_yaml.get("provides_tools") or []):
        checks.append(IntegrationDraftCheckRead(level="warning", code="plugin_tools_empty", message="plugin/plugin.yaml does not expose any tools yet.", path="plugin/plugin.yaml"))

    if not plugin_init_path.exists():
        checks.append(IntegrationDraftCheckRead(level="error", code="plugin_init_missing", message="plugin/__init__.py is missing.", path="plugin/__init__.py"))

    for file_path in sorted(root.rglob("*.py")):
        relative = str(file_path.relative_to(root))
        try:
            py_compile.compile(str(file_path), doraise=True)
        except py_compile.PyCompileError as exc:
            checks.append(IntegrationDraftCheckRead(level="error", code="python_compile_failed", message=str(exc), path=relative))

    skill_md = root / "skill" / "SKILL.md"
    if skill_md.exists():
        checks.append(IntegrationDraftCheckRead(level="info", code="skill_present", message="Companion skill detected.", path="skill/SKILL.md"))
    else:
        checks.append(IntegrationDraftCheckRead(level="warning", code="skill_missing", message="No companion skill found. This is optional but recommended.", path="skill/SKILL.md"))

    valid = not any(check.level == "error" for check in checks)
    if valid:
        checks.insert(0, IntegrationDraftCheckRead(level="info", code="draft_valid", message="Draft validation passed."))
    return _validation_result(valid, checks)


def publish_draft_package(draft: IntegrationDraft) -> dict:
    root = draft_package_root(draft)
    if not root.exists():
        raise ValueError("Draft package root is missing")
    validation = validate_draft(draft)
    if not validation.valid:
        raise ValueError("Draft validation failed. Fix the reported issues before publishing.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
        archive_path = Path(tmp.name)
    try:
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(root, arcname=root.name)
        return install_uploaded_integration_package(archive_path)
    finally:
        archive_path.unlink(missing_ok=True)


def _validation_result(valid: bool, checks: list[IntegrationDraftCheckRead]) -> IntegrationDraftValidationRead:
    return IntegrationDraftValidationRead(
        valid=valid,
        checks=checks,
        validated_at=datetime.now(timezone.utc).isoformat(),
    )


def _validation_from_record(value: dict | None) -> IntegrationDraftValidationRead | None:
    if not isinstance(value, dict):
        return None
    try:
        return IntegrationDraftValidationRead.model_validate(value)
    except Exception:
        return None


def _resolve_draft_file(draft: IntegrationDraft, relative_path: str, *, create_parent: bool = False) -> Path:
    root = draft_package_root(draft).resolve()
    normalized = Path(relative_path.strip())
    if normalized.is_absolute():
        raise ValueError("Draft file path must be relative")
    target = (root / normalized).resolve()
    if root not in target.parents and target != root:
        raise ValueError("Draft file path escapes the draft root")
    if create_parent:
        target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _list_files(root: Path) -> list[IntegrationDraftFileRead]:
    if not root.exists():
        return []
    items: list[IntegrationDraftFileRead] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in {"__pycache__", ".pytest_cache"} for part in path.parts):
            continue
        items.append(
            IntegrationDraftFileRead(
                path=str(path.relative_to(root)),
                size=path.stat().st_size,
            )
        )
    return items


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _empty_template_files(*, package_slug: str, plugin_slug: str, name: str, description: str, version: str) -> dict[str, str]:
    manifest = {
        "slug": package_slug,
        "name": name,
        "description": description or f"Managed {name} integration draft.",
        "version": version,
        "standard_compatible": True,
        "supported_profiles": ["standard", "technical", "security"],
        "required_fields": [],
        "fields": [],
        "defaults": {},
        "plugin_slug": plugin_slug,
        "skill_identifier": f"local/{package_slug}",
        "actions": [],
        "env_map": {},
    }
    return {
        "manifest.yaml": yaml.safe_dump(manifest, sort_keys=False, allow_unicode=False),
        "plugin/__init__.py": (
            "from __future__ import annotations\n\n"
            "def register(ctx):\n"
            "    return None\n"
        ),
        "plugin/plugin.yaml": yaml.safe_dump(
            {
                "name": plugin_slug.replace("_", "-"),
                "version": "1.0.0",
                "description": f"{name} draft plugin",
                "author": "HermesHQ",
                "provides_tools": [],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        "healthcheck.py": (
            "from __future__ import annotations\n\n"
            "async def test_connection(config: dict, resolve_secret):\n"
            "    return True, \"Draft integration scaffold is reachable.\", {\"configured_fields\": sorted(config.keys())}\n"
        ),
        "actions.py": (
            "from __future__ import annotations\n\n"
            "async def run_action(action_slug: str, *, agent, config: dict, resolve_secret, workspaces_root, package_root=None):\n"
            "    return False, f\"Unknown action: {action_slug}\", None\n"
        ),
        "skill/SKILL.md": (
            f"# {name}\n\n"
            "Description: Companion skill for this managed integration draft.\n\n"
            "Use this skill to explain the purpose, caveats, and recommended prompts for the integration.\n"
        ),
    }


def _rest_api_template_files(
    *,
    package_slug: str,
    plugin_slug: str,
    tool_slug: str,
    name: str,
    description: str,
    version: str,
    env_base: str,
) -> dict[str, str]:
    manifest = {
        "slug": package_slug,
        "name": name,
        "description": description or f"Managed {name} integration draft.",
        "version": version,
        "standard_compatible": True,
        "supported_profiles": ["standard", "technical", "security"],
        "required_fields": ["api_key_ref"],
        "fields": [
            {"name": "api_key_ref", "label": f"{name} API key secret", "kind": "secret_ref", "secret_provider": package_slug},
            {"name": "base_url", "label": f"{name} API base URL", "kind": "url", "placeholder": "https://api.example.com"},
        ],
        "defaults": {"base_url": "https://api.example.com"},
        "secret_provider": package_slug,
        "plugin_slug": plugin_slug,
        "skill_identifier": f"local/{package_slug}",
        "test_action": "health",
        "actions": [
            {"slug": "describe_config", "label": "Describe config", "description": "Return the effective integration config."}
        ],
        "env_map": {
            "base_url": f"{env_base}_BASE_URL",
            "api_key_ref": f"{env_base}_API_KEY",
        },
    }
    plugin_init = f"""from __future__ import annotations

import json
import os

import requests

DEFAULT_BASE_URL = "https://api.example.com"
REQUEST_HEADERS = {{
    "Content-Type": "application/json",
    "User-Agent": "HermesHQ-Draft/{package_slug}",
}}


def _configured() -> bool:
    return bool(os.environ.get("{env_base}_API_KEY"))


def _base_url() -> str:
    return os.environ.get("{env_base}_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _headers() -> dict[str, str]:
    api_key = os.environ.get("{env_base}_API_KEY", "").strip()
    headers = dict(REQUEST_HEADERS)
    if api_key:
        headers["Authorization"] = f"Bearer {{api_key}}"
    return headers


def {tool_slug}_tool(args, **_kwargs):
    path = str(args.get("path") or "/").strip() or "/"
    try:
        response = requests.get(f"{{_base_url()}}{{path}}", headers=_headers(), timeout=30)
        body = response.text[:4000] if response.text else ""
        return json.dumps({{
            "success": response.status_code < 400,
            "status_code": response.status_code,
            "path": path,
            "body": body,
        }})
    except Exception as exc:
        return json.dumps({{"success": False, "error": str(exc), "path": path}})


def register(ctx):
    ctx.register_tool(
        name="{tool_slug}",
        toolset="{plugin_slug}",
        schema={{
            "name": "{tool_slug}",
            "description": "Perform a GET request against the configured API path.",
            "parameters": {{
                "type": "object",
                "properties": {{
                    "path": {{
                        "type": "string",
                        "description": "Relative path to call, for example /health or /v1/status.",
                    }}
                }},
            }},
        }},
        handler={tool_slug}_tool,
        check_fn=_configured,
        description="Draft REST API integration tool",
        emoji="🧪",
    )
"""
    healthcheck = f"""from __future__ import annotations

import asyncio

import requests

DEFAULT_BASE_URL = "https://api.example.com"


def _base_url(config: dict) -> str:
    return str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")


async def test_connection(config: dict, resolve_secret):
    secret_ref = str(config.get("api_key_ref") or "").strip()
    if not secret_ref:
        return False, "API key secret is not configured.", None

    api_key = resolve_secret(secret_ref)
    if asyncio.iscoroutine(api_key):
        api_key = await api_key
    if not api_key:
        return False, "Configured API key secret could not be resolved.", None

    try:
        response = requests.get(
            f"{{_base_url(config)}}/",
            headers={{"Authorization": f"Bearer {{api_key}}", "User-Agent": "HermesHQ-Draft/{package_slug}"}},
            timeout=30,
        )
        if response.status_code >= 400:
            return False, f"API returned {{response.status_code}}.", {{"body": response.text[:4000], "base_url": _base_url(config)}}
        return True, "API connection succeeded.", {{"status_code": response.status_code, "base_url": _base_url(config)}}
    except Exception as exc:
        return False, f"API connection failed: {{exc}}", {{"base_url": _base_url(config)}}
"""
    actions = """from __future__ import annotations

async def run_action(action_slug: str, *, agent, config: dict, resolve_secret, workspaces_root, package_root=None):
    if action_slug != "describe_config":
        return False, f"Unknown action: {action_slug}", None
    public_config = {
        key: value
        for key, value in dict(config or {}).items()
        if key not in {"api_key_ref"}
    }
    return True, "Effective config loaded.", {"config": public_config, "agent_id": agent.id}
"""
    skill = (
        f"# {name}\n\n"
        "Description: Companion skill for a managed REST API integration draft.\n\n"
        "Use this skill to document what the API does, the core tools exposed by the plugin, and any usage constraints.\n"
    )
    plugin_yaml = yaml.safe_dump(
        {
            "name": plugin_slug.replace("_", "-"),
            "version": "1.0.0",
            "description": f"{name} draft tools for HermesHQ managed integrations",
            "author": "HermesHQ",
            "provides_tools": [tool_slug],
        },
        sort_keys=False,
        allow_unicode=False,
    )
    return {
        "manifest.yaml": yaml.safe_dump(manifest, sort_keys=False, allow_unicode=False),
        "plugin/__init__.py": plugin_init,
        "plugin/plugin.yaml": plugin_yaml,
        "healthcheck.py": healthcheck,
        "actions.py": actions,
        "skill/SKILL.md": skill,
    }
