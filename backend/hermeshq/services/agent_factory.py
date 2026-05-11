"""Shared service for creating Agent instances.

Centralises the agent-creation logic that was previously duplicated between
``create_agent`` and ``create_agent_from_template`` in the agents router.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.schemas.agent import AgentCreate
from hermeshq.services.agent_identity import (
    derive_agent_identity,
    ensure_unique_agent_slug,
)
from hermeshq.services.managed_capabilities import (
    get_managed_integration,
    list_available_integration_packages,
)
from hermeshq.services.runtime_profiles import get_runtime_profile, normalize_runtime_profile_slug


# ---------------------------------------------------------------------------
# Internal helpers (moved from agents.py)
# ---------------------------------------------------------------------------

def _normalize_integration_configs(value: dict | None) -> dict[str, dict]:
    """Normalise the raw ``integration_configs`` mapping."""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict] = {}
    for slug, config in value.items():
        if not isinstance(slug, str):
            continue
        normalized[slug] = config if isinstance(config, dict) else {}
    return normalized


def _normalize_optional_mode(
    value: str | None,
    *,
    field_name: str,
    allowed: set[str],
) -> str | None:
    """Validate and normalise an optional mode string."""
    normalized = (value or "").strip().lower()
    if not normalized or normalized == "inherit":
        return None
    if normalized not in allowed:
        allowed_values = ", ".join(sorted(["inherit", *allowed]))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name}. Expected one of: {allowed_values}",
        )
    return normalized


_APPROVAL_MODE_OPTIONS = {"off", "on-request", "on-failure"}
_TOOL_PROGRESS_MODE_OPTIONS = {"on", "off"}
_GATEWAY_NOTIFICATIONS_MODE_OPTIONS = {"all", "result", "off"}


def _apply_agent_runtime_behavior_settings(
    agent: Agent,
    values: dict[str, object],
) -> None:
    """Apply approval / tool-progress / gateway-notifications mode overrides."""
    if "approval_mode" in values:
        agent.approval_mode = _normalize_optional_mode(
            values.get("approval_mode"),
            field_name="approval_mode",
            allowed=_APPROVAL_MODE_OPTIONS,
        )
    if "tool_progress_mode" in values:
        agent.tool_progress_mode = _normalize_optional_mode(
            values.get("tool_progress_mode"),
            field_name="tool_progress_mode",
            allowed=_TOOL_PROGRESS_MODE_OPTIONS,
        )
    if "gateway_notifications_mode" in values:
        agent.gateway_notifications_mode = _normalize_optional_mode(
            values.get("gateway_notifications_mode"),
            field_name="gateway_notifications_mode",
            allowed=_GATEWAY_NOTIFICATIONS_MODE_OPTIONS,
        )


def _apply_runtime_profile_defaults(
    agent: Agent,
    profile_slug: str | None,
    *,
    overwrite_toolsets: bool,
) -> None:
    """Fill in runtime-profile defaults on the agent model."""
    profile = get_runtime_profile(profile_slug)
    defaults = profile["defaults"]
    agent.runtime_profile = profile["slug"]
    agent.max_iterations = int(defaults["max_iterations"])
    agent.auto_approve_cmds = bool(defaults["auto_approve_cmds"])
    agent.command_allowlist = list(defaults["command_allowlist"])
    if overwrite_toolsets:
        agent.enabled_toolsets = list(defaults["enabled_toolsets"])
        agent.disabled_toolsets = list(defaults["disabled_toolsets"])


def _sync_agent_integration_toolsets(
    agent: Agent,
    enabled_integration_slugs: list[str],
) -> None:
    """Sync integration toolsets based on the agent's integration configs."""
    known_toolsets = {
        package["plugin_slug"]
        for package in list_available_integration_packages(enabled_integration_slugs)
        if package.get("plugin_slug")
    }
    retained_enabled = [
        toolset for toolset in (agent.enabled_toolsets or [])
        if toolset not in known_toolsets
    ]
    retained_disabled = [
        toolset for toolset in (agent.disabled_toolsets or [])
        if toolset not in known_toolsets
    ]
    for slug in agent.integration_configs or {}:
        integration = get_managed_integration(str(slug), enabled_integration_slugs)
        if integration and integration.get("plugin_slug"):
            retained_enabled.append(str(integration["plugin_slug"]))
    agent.enabled_toolsets = list(dict.fromkeys(retained_enabled))
    agent.disabled_toolsets = list(dict.fromkeys(retained_disabled))


# ---------------------------------------------------------------------------
# Public helpers re-exported for the router
# ---------------------------------------------------------------------------

# Re-export the normalisation helpers so that the router can continue to use
# them without pulling in the private _ prefixed versions above.
normalize_integration_configs = _normalize_integration_configs
normalize_optional_mode = _normalize_optional_mode
apply_agent_runtime_behavior_settings = _apply_agent_runtime_behavior_settings
apply_runtime_profile_defaults = _apply_runtime_profile_defaults
sync_agent_integration_toolsets = _sync_agent_integration_toolsets


# ---------------------------------------------------------------------------
# Runtime defaults resolver
# ---------------------------------------------------------------------------

async def _resolve_runtime_defaults(db: AsyncSession, payload: AgentCreate) -> dict:
    """Resolve model/provider/api_key_ref/base_url defaults from app settings."""
    from hermeshq.models.app_settings import AppSettings

    app_settings = await db.get(AppSettings, "default")
    return {
        "model": payload.model
            or (app_settings.default_model if app_settings else None)
            or "anthropic/claude-sonnet-4",
        "provider": payload.provider
            or (app_settings.default_provider if app_settings else None)
            or "openrouter",
        "api_key_ref": payload.api_key_ref
            or (app_settings.default_api_key_ref if app_settings else None),
        "base_url": payload.base_url
            or (app_settings.default_base_url if app_settings else None),
    }


async def _load_enabled_integration_slugs(db: AsyncSession) -> list[str]:
    """Return the list of enabled integration-package slugs from app settings."""
    from hermeshq.models.app_settings import AppSettings

    app_settings = await db.get(AppSettings, "default")
    enabled = (
        getattr(app_settings, "enabled_integration_packages", [])
        if app_settings
        else []
    )
    return [slug for slug in enabled if isinstance(slug, str) and slug.strip()]


async def _validate_hermes_version(
    hermes_version: str | None,
    *,
    version_manager: object,
) -> str | None:
    """Validate and normalise a Hermes version string.

    Parameters
    ----------
    hermes_version:
        Raw version string from the request payload.
    version_manager:
        The ``hermes_version_manager`` instance attached to the app state.
    """
    normalized = (hermes_version or "").strip() or None
    if normalized == "bundled":
        return None
    if normalized and not version_manager.is_installed(normalized):
        raise HTTPException(
            status_code=400,
            detail=f"Hermes version '{normalized}' is not installed",
        )
    return normalized


# ---------------------------------------------------------------------------
# Core factory function
# ---------------------------------------------------------------------------

async def create_agent_from_config(
    *,
    db: AsyncSession,
    payload: AgentCreate,
    workspace_manager: object,
    hermes_version_manager: object,
) -> Agent:
    """Create a fully initialised :class:`Agent` and persist it.

    This is the single source of truth for agent creation, shared by both
    the direct-creation endpoint and the template-based endpoint.

    Parameters
    ----------
    db:
        Async database session.
    payload:
        Validated creation payload (``AgentCreate`` schema).
    workspace_manager:
        The ``WorkspaceManager`` instance (from ``request.app.state``).
    hermes_version_manager:
        The ``HermesVersionManager`` instance (from ``request.app.state``).

    Returns
    -------
    Agent
        The freshly created and flushed agent record (with ``workspace_path``
        set and the row committed).

    Raises
    ------
    HTTPException
        404 – node not found.
    HTTPException
        400 – invalid hermes version.
    """
    # --- node validation ---------------------------------------------------
    node = await db.get(Node, payload.node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    # --- runtime defaults --------------------------------------------------
    runtime_defaults = await _resolve_runtime_defaults(db, payload)

    # --- identity ----------------------------------------------------------
    friendly_name, name, slug = derive_agent_identity(
        friendly_name=payload.friendly_name,
        name=payload.name,
        slug=payload.slug,
    )
    unique_slug = await ensure_unique_agent_slug(db, slug)

    # --- hermes version ----------------------------------------------------
    hermes_version = await _validate_hermes_version(
        payload.hermes_version,
        version_manager=hermes_version_manager,
    )

    # --- build Agent model -------------------------------------------------
    agent = Agent(
        node_id=payload.node_id,
        name=name,
        friendly_name=friendly_name,
        slug=unique_slug,
        description=payload.description,
        run_mode=payload.run_mode,
        runtime_profile=normalize_runtime_profile_slug(payload.runtime_profile),
        hermes_version=hermes_version,
        approval_mode=None,
        tool_progress_mode=None,
        gateway_notifications_mode=None,
        model=runtime_defaults["model"],
        provider=runtime_defaults["provider"],
        api_key_ref=runtime_defaults["api_key_ref"],
        base_url=runtime_defaults["base_url"],
        system_prompt=payload.system_prompt,
        soul_md=payload.soul_md,
        enabled_toolsets=list(payload.enabled_toolsets or []),
        disabled_toolsets=list(payload.disabled_toolsets or []),
        skills=payload.skills,
        integration_configs=_normalize_integration_configs(payload.integration_configs),
        team_tags=payload.team_tags,
        supervisor_agent_id=payload.supervisor_agent_id,
        workspace_path="pending",
    )

    # --- runtime profile defaults ------------------------------------------
    _apply_runtime_profile_defaults(
        agent,
        payload.runtime_profile,
        overwrite_toolsets=not payload.enabled_toolsets
            and not payload.disabled_toolsets,
    )
    if payload.enabled_toolsets is not None:
        agent.enabled_toolsets = list(payload.enabled_toolsets)
    if payload.disabled_toolsets is not None:
        agent.disabled_toolsets = list(payload.disabled_toolsets)
    if payload.integration_configs is not None:
        agent.integration_configs = _normalize_integration_configs(
            payload.integration_configs,
        )

    # --- behaviour settings ------------------------------------------------
    _apply_agent_runtime_behavior_settings(
        agent,
        {
            "approval_mode": payload.approval_mode,
            "tool_progress_mode": payload.tool_progress_mode,
            "gateway_notifications_mode": payload.gateway_notifications_mode,
        },
    )

    # --- integration toolsets sync -----------------------------------------
    _sync_agent_integration_toolsets(
        agent,
        await _load_enabled_integration_slugs(db),
    )

    # --- persist & workspace -----------------------------------------------
    db.add(agent)
    await db.flush()

    agent.workspace_path = workspace_manager.create_workspace(
        agent.id,
        agent.name,
        payload.system_prompt,
        payload.soul_md,
    )
    await db.commit()
    await db.refresh(agent)

    return agent
