"""Agent CRUD endpoints – create, list, get, update, delete (archive)."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import false, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hermeshq.core.security import ensure_agent_access, get_accessible_agent_ids, get_current_user, is_admin, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.schemas.agent import AgentCreate, AgentRead, AgentUpdate

from hermeshq.routers.agents_shared import (
    USER_EDITABLE_FIELDS,
    _active_agent_clause,
    _apply_agent_runtime_behavior_settings,
    _apply_runtime_profile_defaults,
    _normalize_integration_configs,
    _resolve_runtime_defaults,
    _serialize_agent,
    _sync_agent_integration_toolsets,
    _validate_hermes_version,
    _validate_supervisor,
    _load_enabled_integration_slugs,
)
from hermeshq.services.agent_identity import derive_agent_identity, ensure_unique_agent_slug, slugify_agent_value
from hermeshq.services.runtime_profiles import normalize_runtime_profile_slug
from hermeshq.models.activity import ActivityLog

router = APIRouter(prefix="/agents", tags=["agents"])


# ------------------------------------------------------------------
# LIST
# ------------------------------------------------------------------


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


# ------------------------------------------------------------------
# CREATE
# ------------------------------------------------------------------


@router.post("", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: AgentCreate,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    from hermeshq.models.node import Node

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
    hermes_version = await _validate_hermes_version(request, payload.hermes_version)
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
    _apply_agent_runtime_behavior_settings(
        agent,
        {
            "approval_mode": payload.approval_mode,
            "tool_progress_mode": payload.tool_progress_mode,
            "gateway_notifications_mode": payload.gateway_notifications_mode,
        },
    )
    _sync_agent_integration_toolsets(agent, await _load_enabled_integration_slugs(db))
    db.add(agent)
    await db.flush()
    workspace_manager = request.app.state.workspace_manager
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


# ------------------------------------------------------------------
# GET
# ------------------------------------------------------------------


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


# ------------------------------------------------------------------
# UPDATE
# ------------------------------------------------------------------


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
    hermes_version_changed = "hermes_version" in update_data
    if hermes_version_changed:
        update_data["hermes_version"] = await _validate_hermes_version(request, update_data.get("hermes_version"))
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
    _apply_agent_runtime_behavior_settings(agent, update_data)
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
        "hermes_version",
        "integration_configs",
        "approval_mode",
        "tool_progress_mode",
        "gateway_notifications_mode",
    }
    should_restart_gateways = bool(set(update_data).intersection(restart_gateway_fields))
    should_reset_session = runtime_profile_changed or hermes_version_changed or bool(
        {"approval_mode", "tool_progress_mode", "gateway_notifications_mode"}.intersection(update_data)
    )
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
    if should_reset_session:
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


# ------------------------------------------------------------------
# DELETE (archive)
# ------------------------------------------------------------------


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
