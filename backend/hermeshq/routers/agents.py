import contextlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import false, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hermeshq.config import get_settings
from hermeshq.core.security import ensure_agent_access, get_accessible_agent_ids, get_current_user, is_admin, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.node import Node
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.secret import Secret
from hermeshq.models.task import Task
from hermeshq.models.template import AgentTemplate
from hermeshq.models.user import User
from hermeshq.schemas.agent import AgentCreate, AgentRead, AgentUpdate
from hermeshq.schemas.managed_integration import (
    ManagedIntegrationActionRequest,
    ManagedIntegrationActionResult,
    ManagedIntegrationTestRequest,
    ManagedIntegrationTestResult,
)
from hermeshq.services.agent_identity import derive_agent_identity, ensure_unique_agent_slug, slugify_agent_value
from hermeshq.services.managed_capabilities import get_managed_integration, list_available_integration_packages
from hermeshq.services.managed_integration_actions import ManagedIntegrationActionError, run_managed_integration_action
from hermeshq.services.managed_integration_health import ManagedIntegrationTestError, test_managed_integration
from hermeshq.services.runtime_profiles import get_runtime_profile, normalize_runtime_profile_slug
from hermeshq.services.workspace_manager import WorkspaceManager

router = APIRouter(prefix="/agents", tags=["agents"])
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
ALLOWED_AVATAR_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
MAX_AVATAR_BYTES = 2 * 1024 * 1024


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


def _get_workspace_manager(request: Request) -> WorkspaceManager:
    return request.app.state.workspace_manager


def _get_agent_assets_root() -> Path:
    root = Path(get_settings().agent_assets_root)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _build_avatar_dir(agent_id: str) -> Path:
    return _get_agent_assets_root() / agent_id


def _build_avatar_path(agent: Agent) -> Path | None:
    if not agent.avatar_filename:
        return None
    return _build_avatar_dir(agent.id) / agent.avatar_filename


def _delete_avatar_files(agent_id: str) -> None:
    avatar_dir = _build_avatar_dir(agent_id)
    if not avatar_dir.exists():
        return
    for path in sorted(avatar_dir.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    avatar_dir.rmdir()


def _serialize_agent(request: Request, agent: Agent) -> AgentRead:
    payload = AgentRead.model_validate(agent)
    avatar_url = None
    if agent.avatar_filename:
        version = int(agent.updated_at.timestamp()) if agent.updated_at else 0
        avatar_url = f"{get_settings().api_prefix}/agents/{agent.id}/avatar?v={version}"
    return payload.model_copy(update={"avatar_url": avatar_url, "has_avatar": bool(agent.avatar_filename)})


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


@router.get("", response_model=list[AgentRead])
async def list_agents(
    request: Request,
    include_archived: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[AgentRead]:
    statement = select(Agent).options(selectinload(Agent.node)).order_by(Agent.created_at.asc())
    if not include_archived:
        statement = statement.where(_active_agent_clause())
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(Agent.id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    result = await db.execute(statement)
    return [_serialize_agent(request, agent) for agent in result.scalars().all()]


@router.post("", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    node = await db.get(Node, payload.node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    await _validate_supervisor(db, None, payload.supervisor_agent_id)
    runtime_defaults = await _resolve_runtime_defaults(db, payload)
    friendly_name, name, slug = derive_agent_identity(
        friendly_name=payload.friendly_name,
        name=payload.name,
        slug=payload.slug,
    )
    unique_slug = await ensure_unique_agent_slug(db, slug)
    agent = Agent(
        node_id=payload.node_id,
        name=name,
        friendly_name=friendly_name,
        slug=unique_slug,
        description=payload.description,
        run_mode=payload.run_mode,
        runtime_profile=normalize_runtime_profile_slug(payload.runtime_profile),
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
    _apply_runtime_profile_defaults(
        agent,
        payload.runtime_profile,
        overwrite_toolsets=not payload.enabled_toolsets and not payload.disabled_toolsets,
    )
    if payload.enabled_toolsets is not None:
        agent.enabled_toolsets = list(payload.enabled_toolsets)
    if payload.disabled_toolsets is not None:
        agent.disabled_toolsets = list(payload.disabled_toolsets)
    if payload.integration_configs is not None:
        agent.integration_configs = _normalize_integration_configs(payload.integration_configs)
    _sync_agent_integration_toolsets(agent, await _load_enabled_integration_slugs(db))
    db.add(agent)
    await db.flush()
    workspace_manager = _get_workspace_manager(request)
    agent.workspace_path = workspace_manager.create_workspace(
        agent.id,
        agent.name,
        payload.system_prompt,
        payload.soul_md,
    )
    await db.commit()
    await db.refresh(agent)
    await request.app.state.installation_manager.sync_agent_installation(agent)
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent.id))
    created = result.scalar_one_or_none() or agent
    return _serialize_agent(request, created)


@router.get("/{agent_id}", response_model=AgentRead)
async def get_agent(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    await ensure_agent_access(db, current_user, agent_id)
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    agent = result.scalar_one()
    return _serialize_agent(request, agent)


@router.put("/{agent_id}", response_model=AgentRead)
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    agent = await ensure_agent_access(db, current_user, agent_id)
    update_data = payload.model_dump(exclude_unset=True)
    if not is_admin(current_user):
        restricted_fields = sorted(set(update_data) - USER_EDITABLE_FIELDS)
        if restricted_fields:
            raise HTTPException(
                status_code=403,
                detail=f"Users cannot modify: {', '.join(restricted_fields)}",
            )
    if "supervisor_agent_id" in update_data:
        await _validate_supervisor(db, agent_id, update_data.get("supervisor_agent_id"))
    runtime_profile_changed = "runtime_profile" in update_data
    current_friendly = (agent.friendly_name or "").strip()
    current_name = (agent.name or "").strip()
    current_slug = (agent.slug or "").strip()
    current_derived_slug = slugify_agent_value(current_friendly or current_name)

    requested_friendly = update_data.get("friendly_name", agent.friendly_name)
    requested_name = update_data.get("name", agent.name)
    requested_slug = update_data.get("slug", agent.slug)

    if "friendly_name" in update_data and "name" not in update_data:
        if not current_name or current_name == current_friendly:
            requested_name = requested_friendly
    if "slug" not in update_data:
        if not current_slug or current_slug == current_derived_slug:
            requested_slug = requested_friendly or requested_name

    resolved_friendly, resolved_name, resolved_slug = derive_agent_identity(
        friendly_name=requested_friendly,
        name=requested_name,
        slug=requested_slug,
    )
    unique_slug = await ensure_unique_agent_slug(db, resolved_slug, exclude_agent_id=agent_id)

    for field, value in update_data.items():
        if field == "integration_configs":
            continue
        setattr(agent, field, value)
    if "integration_configs" in update_data:
        agent.integration_configs = _normalize_integration_configs(update_data.get("integration_configs"))
    if runtime_profile_changed:
        _apply_runtime_profile_defaults(
            agent,
            update_data.get("runtime_profile"),
            overwrite_toolsets="enabled_toolsets" not in update_data and "disabled_toolsets" not in update_data,
        )
    _sync_agent_integration_toolsets(agent, await _load_enabled_integration_slugs(db))
    agent.friendly_name = resolved_friendly
    agent.name = resolved_name
    agent.slug = unique_slug
    restart_gateway_fields = {
        "name",
        "friendly_name",
        "slug",
        "description",
        "system_prompt",
        "soul_md",
        "skills",
        "team_tags",
        "supervisor_agent_id",
        "can_send_tasks",
        "can_receive_tasks",
        "runtime_profile",
        "integration_configs",
    }
    should_restart_gateways = bool(set(update_data).intersection(restart_gateway_fields))
    if any(
        field in update_data
        for field in ("name", "friendly_name", "slug", "system_prompt", "soul_md")
    ):
        request.app.state.workspace_manager.sync_config(
            agent.id,
            agent.name,
            agent.system_prompt,
            agent.soul_md,
        )
    await db.commit()
    await request.app.state.installation_manager.sync_agent_installation(agent)
    if runtime_profile_changed:
        await request.app.state.pty_manager.destroy_session(agent_id)
    if should_restart_gateways:
        channel_result = await db.execute(
            select(MessagingChannel.platform, MessagingChannel.enabled).where(MessagingChannel.agent_id == agent_id)
        )
        for platform, enabled in channel_result.all():
            if enabled:
                await request.app.state.gateway_supervisor.restart_channel(agent_id, platform)
    result = await db.execute(
        select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id)
    )
    return _serialize_agent(request, result.scalar_one())


@router.post("/{agent_id}/integrations/{integration_slug}/test", response_model=ManagedIntegrationTestResult)
async def test_agent_integration(
    agent_id: str,
    integration_slug: str,
    payload: ManagedIntegrationTestRequest,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> ManagedIntegrationTestResult:
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    async def _resolve_secret(secret_ref: str) -> str | None:
        result = await db.execute(select(Secret).where(Secret.name == secret_ref))
        secret = result.scalar_one_or_none()
        if not secret:
            return None
        return request.app.state.secret_vault.decrypt(secret.value_enc)

    try:
        enabled_integration_slugs = await _load_enabled_integration_slugs(db)
        success, message, details = await test_managed_integration(
            agent,
            integration_slug,
            payload.config or {},
            enabled_integration_slugs,
            _resolve_secret,
        )
        return ManagedIntegrationTestResult(success=success, message=message, details=details)
    except ManagedIntegrationTestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{agent_id}/integrations/{integration_slug}/actions/{action_slug}", response_model=ManagedIntegrationActionResult)
async def run_agent_integration_action(
    agent_id: str,
    integration_slug: str,
    action_slug: str,
    payload: ManagedIntegrationActionRequest,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> ManagedIntegrationActionResult:
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    async def _resolve_secret(secret_ref: str) -> str | None:
        result = await db.execute(select(Secret).where(Secret.name == secret_ref))
        secret = result.scalar_one_or_none()
        if not secret:
            return None
        return request.app.state.secret_vault.decrypt(secret.value_enc)

    try:
        enabled_integration_slugs = await _load_enabled_integration_slugs(db)
        success, message, details = await run_managed_integration_action(
            agent,
            integration_slug,
            action_slug,
            payload.config or {},
            enabled_integration_slugs,
            _resolve_secret,
        )
    except ManagedIntegrationActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    issue_count = 0
    if isinstance(details, dict):
        issue_count = int(details.get("issue_count") or 0)
    severity = "error" if not success else "warning" if issue_count else "info"
    db.add(
        ActivityLog(
            agent_id=agent.id,
            event_type=f"security.{integration_slug}.{action_slug}",
            severity=severity,
            message=message,
            details={
                "integration_slug": integration_slug,
                "action_slug": action_slug,
                "success": success,
                "summary": details,
            },
        )
    )
    await db.commit()
    return ManagedIntegrationActionResult(success=success, message=message, details=details)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_archived:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    active_task_ids = list(
        (
            await db.execute(
                select(Task.id).where(
                    Task.agent_id == agent_id,
                    Task.status.in_(["queued", "running"]),
                )
            )
        ).scalars()
    )

    supervisor = request.app.state.supervisor
    if agent.status == "running":
        try:
            await supervisor.stop_agent(agent_id)
        except ValueError:
            pass
    for task_id in active_task_ids:
        await supervisor.cancel_task(task_id)
    channel_platforms = list(
        (
            await db.execute(
                select(MessagingChannel.platform).where(MessagingChannel.agent_id == agent_id)
            )
        ).scalars()
    )
    for platform in channel_platforms:
        with contextlib.suppress(Exception):
            await request.app.state.gateway_supervisor.stop_channel(agent_id, platform)

    await db.execute(
        update(Agent)
        .where(Agent.supervisor_agent_id == agent_id)
        .values(supervisor_agent_id=None)
    )
    await db.execute(
        update(Task)
        .where(Task.agent_id == agent_id, Task.status == "queued")
        .values(status="cancelled", error_message="Agent archived", completed_at=func.now())
    )
    await db.execute(
        update(Task)
        .where(Task.agent_id == agent_id, Task.status == "running")
        .values(status="cancelled", error_message="Agent archived", completed_at=func.now())
    )
    await db.execute(
        update(MessagingChannel)
        .where(MessagingChannel.agent_id == agent_id)
        .values(enabled=False, status="stopped")
    )
    await db.execute(
        update(ScheduledTask)
        .where(ScheduledTask.agent_id == agent_id)
        .values(enabled=False)
    )

    agent.status = "stopped"
    agent.is_archived = True
    agent.archived_at = datetime.now(timezone.utc)
    agent.archive_reason = f"Archived by {current_user.username}"
    agent.last_activity = agent.archived_at
    db.add(
        ActivityLog(
            agent_id=agent.id,
            event_type="agent.archived",
            severity="info",
            message=f"{agent.name} archived",
            details={"archived_by": current_user.username},
        )
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/from-template/{template_id}", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
async def create_agent_from_template(
    template_id: str,
    request: Request,
    payload: dict = Body(default={}),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    template = await db.get(AgentTemplate, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    merged_payload = {**template.config, **payload}
    agent_payload = AgentCreate(**merged_payload)
    node = await db.get(Node, agent_payload.node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    await _validate_supervisor(db, None, agent_payload.supervisor_agent_id)
    runtime_defaults = await _resolve_runtime_defaults(db, agent_payload)
    friendly_name, name, slug = derive_agent_identity(
        friendly_name=agent_payload.friendly_name,
        name=agent_payload.name,
        slug=agent_payload.slug,
    )
    unique_slug = await ensure_unique_agent_slug(db, slug)
    agent = Agent(
        node_id=agent_payload.node_id,
        name=name,
        friendly_name=friendly_name,
        slug=unique_slug,
        description=agent_payload.description,
        run_mode=agent_payload.run_mode,
        runtime_profile=normalize_runtime_profile_slug(agent_payload.runtime_profile),
        model=runtime_defaults["model"],
        provider=runtime_defaults["provider"],
        api_key_ref=runtime_defaults["api_key_ref"],
        base_url=runtime_defaults["base_url"],
        system_prompt=agent_payload.system_prompt,
        soul_md=agent_payload.soul_md,
        enabled_toolsets=list(agent_payload.enabled_toolsets or []),
        disabled_toolsets=list(agent_payload.disabled_toolsets or []),
        skills=agent_payload.skills,
        integration_configs=_normalize_integration_configs(agent_payload.integration_configs),
        team_tags=agent_payload.team_tags,
        supervisor_agent_id=agent_payload.supervisor_agent_id,
        workspace_path="pending",
    )
    _apply_runtime_profile_defaults(
        agent,
        agent_payload.runtime_profile,
        overwrite_toolsets=not agent_payload.enabled_toolsets and not agent_payload.disabled_toolsets,
    )
    if agent_payload.enabled_toolsets is not None:
        agent.enabled_toolsets = list(agent_payload.enabled_toolsets)
    if agent_payload.disabled_toolsets is not None:
        agent.disabled_toolsets = list(agent_payload.disabled_toolsets)
    if agent_payload.integration_configs is not None:
        agent.integration_configs = _normalize_integration_configs(agent_payload.integration_configs)
    _sync_agent_integration_toolsets(agent, await _load_enabled_integration_slugs(db))
    db.add(agent)
    await db.flush()
    agent.workspace_path = request.app.state.workspace_manager.create_workspace(
        agent.id,
        agent.name,
        agent.system_prompt,
        agent.soul_md,
    )
    await db.commit()
    await db.refresh(agent)
    await request.app.state.installation_manager.sync_agent_installation(agent)
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent.id))
    created = result.scalar_one_or_none() or agent
    return _serialize_agent(request, created)


@router.post("/{agent_id}/start", response_model=AgentRead)
async def start_agent(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    await ensure_agent_access(db, current_user, agent_id)
    supervisor = request.app.state.supervisor
    try:
        await supervisor.start_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await db.execute(
        select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id)
    )
    return _serialize_agent(request, result.scalar_one())


@router.post("/{agent_id}/stop", response_model=AgentRead)
async def stop_agent(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    await ensure_agent_access(db, current_user, agent_id)
    supervisor = request.app.state.supervisor
    await supervisor.stop_agent(agent_id)
    result = await db.execute(
        select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id)
    )
    return _serialize_agent(request, result.scalar_one())


@router.post("/{agent_id}/restart", response_model=AgentRead)
async def restart_agent(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    await ensure_agent_access(db, current_user, agent_id)
    supervisor = request.app.state.supervisor
    try:
        await supervisor.restart_agent(agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await db.execute(
        select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id)
    )
    return _serialize_agent(request, result.scalar_one())


@router.post("/{agent_id}/mode", response_model=AgentRead)
async def set_agent_mode(
    agent_id: str,
    payload: dict,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    agent = await ensure_agent_access(db, current_user, agent_id)
    mode = payload.get("mode")
    if mode not in {"headless", "interactive", "hybrid"}:
        raise HTTPException(status_code=400, detail="Invalid mode")
    agent.run_mode = mode
    await db.commit()
    result = await db.execute(
        select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id)
    )
    return _serialize_agent(request, result.scalar_one())


@router.get("/{agent_id}/avatar", include_in_schema=False)
async def get_agent_avatar(agent_id: str, db: AsyncSession = Depends(get_db_session)):
    agent = await db.get(Agent, agent_id)
    if not agent or not agent.avatar_filename:
        raise HTTPException(status_code=404, detail="Avatar not found")
    avatar_path = _build_avatar_path(agent)
    if not avatar_path or not avatar_path.exists():
        raise HTTPException(status_code=404, detail="Avatar not found")
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(avatar_path.suffix.lower(), "application/octet-stream")
    return FileResponse(avatar_path, media_type=media_type)


@router.post("/{agent_id}/avatar", response_model=AgentRead)
async def upload_agent_avatar(
    agent_id: str,
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    agent = await ensure_agent_access(db, current_user, agent_id)
    if file.content_type not in ALLOWED_AVATAR_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported avatar type. Use PNG, JPG or WEBP.")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Avatar file is empty")
    if len(content) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="Avatar exceeds 2 MB limit")
    avatar_dir = _build_avatar_dir(agent_id)
    avatar_dir.mkdir(parents=True, exist_ok=True)
    for existing in avatar_dir.iterdir():
        if existing.is_file() or existing.is_symlink():
            existing.unlink()
    extension = ALLOWED_AVATAR_TYPES[file.content_type]
    filename = f"avatar{extension}"
    avatar_path = avatar_dir / filename
    avatar_path.write_bytes(content)
    agent.avatar_filename = filename
    await db.commit()
    await db.refresh(agent)
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    return _serialize_agent(request, result.scalar_one())


@router.delete("/{agent_id}/avatar", response_model=AgentRead)
async def delete_agent_avatar(
    agent_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    agent = await ensure_agent_access(db, current_user, agent_id)
    _delete_avatar_files(agent_id)
    agent.avatar_filename = None
    await db.commit()
    await db.refresh(agent)
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    return _serialize_agent(request, result.scalar_one())


@router.get("/{agent_id}/workspace")
async def list_workspace(
    agent_id: str,
    request: Request,
    path: str = Query(default="."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await ensure_agent_access(db, current_user, agent_id)
    return {
        "entries": request.app.state.workspace_manager.list_workspace_files(agent_id, path),
        "size": request.app.state.workspace_manager.get_workspace_size(agent_id),
    }


@router.get("/{agent_id}/workspace/{file_path:path}")
async def read_workspace_file(
    agent_id: str,
    file_path: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await ensure_agent_access(db, current_user, agent_id)
    return {"path": file_path, "content": request.app.state.workspace_manager.read_workspace_file(agent_id, file_path)}


@router.put("/{agent_id}/workspace/{file_path:path}")
async def write_workspace_file(
    agent_id: str,
    file_path: str,
    payload: dict,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await ensure_agent_access(db, current_user, agent_id)
    request.app.state.workspace_manager.write_workspace_file(agent_id, file_path, payload.get("content", ""))
    return {"status": "ok", "path": file_path}
