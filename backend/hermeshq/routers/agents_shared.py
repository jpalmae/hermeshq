"""Shared constants and helper functions used across agent sub-routers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.core.security import get_accessible_agent_ids, is_admin
from hermeshq.models.agent import Agent
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.conversation_thread import ConversationThread
from hermeshq.models.node import Node
from hermeshq.models.provider import ProviderDefinition
from hermeshq.models.task import Task
from hermeshq.schemas.agent import AgentCreate, AgentRead
from hermeshq.services.agent_identity import derive_agent_identity, ensure_unique_agent_slug, slugify_agent_value
from hermeshq.services.avatar import build_avatar_path as _build_avatar_path_shared
from hermeshq.services.managed_capabilities import get_managed_integration, list_available_integration_packages
from hermeshq.services.runtime_profiles import get_runtime_profile, normalize_runtime_profile_slug
from hermeshq.services.task_board import next_board_order, runtime_status_to_board_column
from hermeshq.services.workspace_manager import WorkspaceManager

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_EDITABLE_FIELDS = {
    "name",
    "friendly_name",
    "slug",
    "description",
    "run_mode",
    "system_prompt",
    "soul_md",
    "skills",
    "team_tags",
}

MAX_BULK_AGENT_TARGETS = 25

APPROVAL_MODE_OPTIONS = {"off", "on-request", "on-failure"}

TOOL_PROGRESS_MODE_OPTIONS = {"on", "off"}

GATEWAY_NOTIFICATIONS_MODE_OPTIONS = {"all", "result", "off"}

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _active_agent_clause():
    return Agent.is_archived.is_(False)


def _normalize_integration_configs(value: dict | None) -> dict[str, dict]:
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
    normalized = (value or "").strip().lower()
    if not normalized or normalized == "inherit":
        return None
    if normalized not in allowed:
        allowed_values = ", ".join(sorted(["inherit", *allowed]))
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}. Expected one of: {allowed_values}")
    return normalized


def _apply_agent_runtime_behavior_settings(agent: Agent, values: dict[str, object]) -> None:
    if "approval_mode" in values:
        agent.approval_mode = _normalize_optional_mode(
            values.get("approval_mode"),
            field_name="approval_mode",
            allowed=APPROVAL_MODE_OPTIONS,
        )
    if "tool_progress_mode" in values:
        agent.tool_progress_mode = _normalize_optional_mode(
            values.get("tool_progress_mode"),
            field_name="tool_progress_mode",
            allowed=TOOL_PROGRESS_MODE_OPTIONS,
        )
    if "gateway_notifications_mode" in values:
        agent.gateway_notifications_mode = _normalize_optional_mode(
            values.get("gateway_notifications_mode"),
            field_name="gateway_notifications_mode",
            allowed=GATEWAY_NOTIFICATIONS_MODE_OPTIONS,
        )


def _get_workspace_manager(request: Request) -> WorkspaceManager:
    return request.app.state.workspace_manager


def _agent_avatar_base() -> Path:
    return Path(get_settings().agent_assets_root)


def _build_avatar_path(agent: Agent) -> Path | None:
    return _build_avatar_path_shared(_agent_avatar_base(), agent.id, agent.avatar_filename)


def _serialize_agent(request: Request, agent: Agent) -> AgentRead:
    payload = AgentRead.model_validate(agent)
    avatar_url = None
    if agent.avatar_filename:
        version = int(agent.updated_at.timestamp()) if agent.updated_at else 0
        avatar_url = f"{get_settings().api_prefix}/agents/{agent.id}/avatar?v={version}"
    return payload.model_copy(update={"avatar_url": avatar_url, "has_avatar": bool(agent.avatar_filename)})


async def _load_bulk_agents(
    db: AsyncSession,
    current_user,
    agent_ids: list[str],
) -> list[Agent]:
    from hermeshq.models.user import User  # avoid circular-ish import at module level is fine

    ordered_ids = list(dict.fromkeys(str(agent_id).strip() for agent_id in agent_ids if str(agent_id).strip()))
    if not ordered_ids:
        raise HTTPException(status_code=400, detail="Select at least one agent")
    if len(ordered_ids) > MAX_BULK_AGENT_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"Bulk operations are limited to {MAX_BULK_AGENT_TARGETS} agents per request",
        )

    statement = select(Agent).where(Agent.id.in_(ordered_ids))
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(Agent.id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    result = await db.execute(statement)
    agents = result.scalars().all()
    agent_map = {agent.id: agent for agent in agents}
    if len(agent_map) != len(ordered_ids):
        raise HTTPException(status_code=404, detail="One or more agents were not found or are not accessible")
    return [agent_map[agent_id] for agent_id in ordered_ids]


async def _auto_start_agent_if_needed(
    db: AsyncSession,
    request: Request,
    agent: Agent,
    *,
    auto_start_stopped: bool,
) -> str | None:
    if not auto_start_stopped or agent.status == "running":
        return None
    try:
        await request.app.state.supervisor.start_agent(agent.id)
    except ValueError as exc:
        return str(exc)
    await db.refresh(agent)
    return None


async def _create_conversation_task(
    db: AsyncSession,
    *,
    agent: Agent,
    current_user,
    prompt: str,
    metadata: dict,
) -> Task:
    result = await db.execute(
        select(ConversationThread).where(
            ConversationThread.agent_id == agent.id,
            ConversationThread.user_id == current_user.id,
        )
    )
    thread = result.scalar_one_or_none()
    if not thread:
        thread = ConversationThread(
            agent_id=agent.id,
            user_id=current_user.id,
            title=(prompt[:80]).strip() or "Conversation",
        )
        db.add(thread)
        await db.flush()

    task_metadata = {
        **metadata,
        "conversation": True,
        "thread_id": thread.id,
        "thread_user_id": current_user.id,
    }
    task = Task(
        agent_id=agent.id,
        title="Chat message",
        prompt=prompt,
        priority=5,
        metadata_json=task_metadata,
    )
    task.board_column = runtime_status_to_board_column(task.status)
    task.board_order = next_board_order()
    task.board_manual = False
    db.add(task)
    await db.flush()
    thread.last_task_id = task.id
    return task


async def _validate_supervisor(
    db: AsyncSession,
    agent_id: str | None,
    supervisor_agent_id: str | None,
) -> None:
    if not supervisor_agent_id:
        return
    if agent_id and supervisor_agent_id == agent_id:
        raise HTTPException(status_code=400, detail="Agent cannot supervise itself")
    supervisor = await db.get(Agent, supervisor_agent_id)
    if not supervisor:
        raise HTTPException(status_code=404, detail="Supervisor agent not found")
    current_parent_id = supervisor.supervisor_agent_id
    seen: set[str] = set()
    while current_parent_id:
        if current_parent_id in seen:
            break
        if agent_id and current_parent_id == agent_id:
            raise HTTPException(status_code=400, detail="Hierarchy cycle detected")
        seen.add(current_parent_id)
        parent = await db.get(Agent, current_parent_id)
        current_parent_id = parent.supervisor_agent_id if parent else None


async def _resolve_runtime_defaults(db: AsyncSession, payload: AgentCreate) -> dict:
    app_settings = await db.get(AppSettings, "default")
    return {
        "model": payload.model or (app_settings.default_model if app_settings else None) or "anthropic/claude-sonnet-4",
        "provider": payload.provider or (app_settings.default_provider if app_settings else None) or "openrouter",
        "api_key_ref": payload.api_key_ref or (app_settings.default_api_key_ref if app_settings else None),
        "base_url": payload.base_url or (app_settings.default_base_url if app_settings else None),
    }


def _apply_runtime_profile_defaults(
    agent: Agent,
    profile_slug: str | None,
    *,
    overwrite_toolsets: bool,
) -> None:
    profile = get_runtime_profile(profile_slug)
    defaults = profile["defaults"]
    agent.runtime_profile = profile["slug"]
    agent.max_iterations = int(defaults["max_iterations"])
    agent.auto_approve_cmds = bool(defaults["auto_approve_cmds"])
    agent.command_allowlist = list(defaults["command_allowlist"])
    if overwrite_toolsets:
        agent.enabled_toolsets = list(defaults["enabled_toolsets"])
        agent.disabled_toolsets = list(defaults["disabled_toolsets"])


async def _load_enabled_integration_slugs(db: AsyncSession) -> list[str]:
    app_settings = await db.get(AppSettings, "default")
    enabled = getattr(app_settings, "enabled_integration_packages", []) if app_settings else []
    return [slug for slug in enabled if isinstance(slug, str) and slug.strip()]


async def _validate_hermes_version(request: Request, hermes_version: str | None) -> str | None:
    normalized = (hermes_version or "").strip() or None
    if normalized == "bundled":
        return None
    if normalized and not request.app.state.hermes_version_manager.is_installed(normalized):
        raise HTTPException(status_code=400, detail=f"Hermes version '{normalized}' is not installed")
    return normalized


def _sync_agent_integration_toolsets(agent: Agent, enabled_integration_slugs: list[str]) -> None:
    known_toolsets = {
        package["plugin_slug"]
        for package in list_available_integration_packages(enabled_integration_slugs)
        if package.get("plugin_slug")
    }
    retained_enabled = [toolset for toolset in (agent.enabled_toolsets or []) if toolset not in known_toolsets]
    retained_disabled = [toolset for toolset in (agent.disabled_toolsets or []) if toolset not in known_toolsets]
    for slug in (agent.integration_configs or {}):
        integration = get_managed_integration(str(slug), enabled_integration_slugs)
        if integration and integration.get("plugin_slug"):
            retained_enabled.append(str(integration["plugin_slug"]))
    agent.enabled_toolsets = list(dict.fromkeys(retained_enabled))
    agent.disabled_toolsets = list(dict.fromkeys(retained_disabled))
