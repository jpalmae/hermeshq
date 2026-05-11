"""Agent template endpoints – create from template, system operator bootstrap."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.node import Node
from hermeshq.models.provider import ProviderDefinition
from hermeshq.models.template import AgentTemplate
from hermeshq.models.user import User
from hermeshq.schemas.agent import AgentCreate, AgentRead

from hermeshq.routers.agents_shared import (
    _apply_agent_runtime_behavior_settings,
    _apply_runtime_profile_defaults,
    _load_enabled_integration_slugs,
    _normalize_integration_configs,
    _resolve_runtime_defaults,
    _serialize_agent,
    _sync_agent_integration_toolsets,
    _validate_hermes_version,
    _validate_supervisor,
)
from hermeshq.services.agent_identity import derive_agent_identity, ensure_unique_agent_slug
from hermeshq.services.runtime_profiles import normalize_runtime_profile_slug
from hermeshq.models.activity import ActivityLog

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/system/operator/bootstrap", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
async def bootstrap_system_operator(
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    result = await db.execute(
        select(Agent)
        .options(selectinload(Agent.node))
        .where(Agent.is_system_agent.is_(True), Agent.slug == "hq-operator")
        .order_by(Agent.created_at.asc())
    )
    existing = result.scalar_one_or_none()

    app_settings = await db.get(AppSettings, "default")
    default_provider = (app_settings.default_provider or "").strip() if app_settings else ""
    default_model = (app_settings.default_model or "").strip() if app_settings else ""
    default_api_key_ref = (app_settings.default_api_key_ref or "").strip() if app_settings else ""
    default_base_url = (app_settings.default_base_url or "").strip() if app_settings else ""
    default_hermes_version = (app_settings.default_hermes_version or "").strip() if app_settings else ""
    if not default_provider or not default_model:
        raise HTTPException(
            status_code=400,
            detail="Configure default provider and model first so HQ Operator can use inference",
        )
    provider = await db.get(ProviderDefinition, default_provider)
    if provider and provider.auth_type == "api_key" and provider.supports_secret_ref and not default_api_key_ref:
        raise HTTPException(
            status_code=400,
            detail="Configure a default secret ref first so HQ Operator can authenticate",
        )
    local_runtime_result = await db.execute(select(Node).where(Node.name == "Local Runtime").order_by(Node.created_at.asc()))
    node = local_runtime_result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Local Runtime node not found")

    if existing:
        existing.is_archived = False
        existing.archived_at = None
        existing.archive_reason = None
        existing.status = "stopped"
        existing.last_activity = None
        existing.node_id = node.id
        existing.run_mode = "hybrid"
        existing.runtime_profile = "technical"
        existing.provider = default_provider
        existing.model = default_model
        existing.api_key_ref = default_api_key_ref or None
        existing.base_url = default_base_url or None
        existing.hermes_version = default_hermes_version or None
        existing.approval_mode = None
        existing.tool_progress_mode = None
        existing.gateway_notifications_mode = None
        existing.is_system_agent = True
        existing.system_scope = "admin"
        existing.team_tags = ["system", "control-plane", "operations"]
        existing.enabled_toolsets = []
        existing.disabled_toolsets = []
        existing.can_send_tasks = True
        existing.can_receive_tasks = True
        existing.description = "HermesHQ control-plane operator with administrative tools and shell access."
        existing.system_prompt = (
            "You are HQ Operator, the HermesHQ system operations agent. "
            "Use HermesHQ control tools for control-plane changes first, keep actions explicit, "
            "and use shell only when the administrative tools do not cover the task."
        )
        existing.soul_md = "# Soul\n\nControl-plane operator for HermesHQ."
        await db.commit()
        await request.app.state.installation_manager.sync_agent_installation(existing)
        refreshed = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == existing.id))
        return _serialize_agent(request, refreshed.scalar_one())

    agent = Agent(
        node_id=node.id,
        name="hq-operator",
        friendly_name="HQ Operator",
        slug="hq-operator",
        description="HermesHQ control-plane operator with administrative tools and shell access.",
        run_mode="hybrid",
        runtime_profile="technical",
        hermes_version=default_hermes_version or None,
        approval_mode=None,
        tool_progress_mode=None,
        gateway_notifications_mode=None,
        model=default_model,
        provider=default_provider,
        api_key_ref=default_api_key_ref or None,
        base_url=default_base_url or None,
        system_prompt=(
            "You are HQ Operator, the HermesHQ system operations agent. "
            "Use HermesHQ control tools for control-plane changes first, keep actions explicit, "
            "and use shell only when the administrative tools do not cover the task."
        ),
        soul_md="# Soul\n\nControl-plane operator for HermesHQ.",
        enabled_toolsets=[],
        disabled_toolsets=[],
        skills=[],
        integration_configs={},
        team_tags=["system", "control-plane", "operations"],
        workspace_path="pending",
        is_system_agent=True,
        system_scope="admin",
        can_send_tasks=True,
        can_receive_tasks=True,
    )
    _apply_runtime_profile_defaults(agent, "technical", overwrite_toolsets=True)
    agent.enabled_toolsets = []
    agent.disabled_toolsets = []
    db.add(agent)
    await db.flush()
    workspace_manager = request.app.state.workspace_manager
    agent.workspace_path = workspace_manager.create_workspace(
        agent.id,
        agent.name,
        agent.system_prompt,
        agent.soul_md,
    )
    await db.commit()
    await db.refresh(agent)
    await request.app.state.installation_manager.sync_agent_installation(agent)
    created_result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent.id))
    created = created_result.scalar_one_or_none() or agent
    db.add(
        ActivityLog(
            agent_id=created.id,
            event_type="agent.system_operator.created",
            severity="info",
            message="HQ Operator bootstrapped",
            details={"system_scope": "admin", "runtime_profile": "technical"},
        )
    )
    await db.commit()
    return _serialize_agent(request, created)


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
    hermes_version = await _validate_hermes_version(request, agent_payload.hermes_version)
    agent = Agent(
        node_id=agent_payload.node_id,
        name=name,
        friendly_name=friendly_name,
        slug=unique_slug,
        description=agent_payload.description,
        run_mode=agent_payload.run_mode,
        runtime_profile=normalize_runtime_profile_slug(agent_payload.runtime_profile),
        hermes_version=hermes_version,
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
