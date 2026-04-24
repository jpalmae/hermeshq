from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from hermeshq.database import get_db_session
from hermeshq.core.security import create_agent_service_token
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.provider import ProviderDefinition
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.secret import Secret
from hermeshq.models.user import User
from hermeshq.routers import agents as agents_router
from hermeshq.routers import integration_factory as integration_factory_router
from hermeshq.routers import integration_packages as integration_packages_router
from hermeshq.routers import providers as providers_router
from hermeshq.routers import scheduled_tasks as scheduled_tasks_router
from hermeshq.routers import secrets as secrets_router
from hermeshq.routers import users as users_router
from hermeshq.schemas.agent import AgentCreate, AgentRead, AgentUpdate
from hermeshq.schemas.integration_factory import (
    IntegrationDraftCreate,
    IntegrationDraftFileContentRead,
    IntegrationDraftFileUpdate,
    IntegrationDraftPublishRead,
    IntegrationDraftRead,
    IntegrationDraftUpdate,
    IntegrationDraftValidationRead,
)
from hermeshq.schemas.managed_integration import (
    ManagedIntegrationRead,
    ManagedIntegrationTestRequest,
    ManagedIntegrationTestResult,
)
from hermeshq.schemas.provider import ProviderRead, ProviderUpdate
from hermeshq.schemas.scheduled_task import ScheduledTaskCreate, ScheduledTaskRead, ScheduledTaskUpdate
from hermeshq.schemas.secret import SecretCreate, SecretRead, SecretUpdate
from hermeshq.schemas.user_management import UserCreate, UserManagedRead, UserUpdate
from hermeshq.services.managed_capabilities import get_managed_integration

router = APIRouter(prefix="/internal/control", tags=["internal-control"], include_in_schema=False)


class ArchiveAgentRequest(BaseModel):
    reason: str | None = None


class ProviderCreateRequest(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    runtime_provider: str = Field(min_length=1, max_length=64)
    auth_type: str = "api_key"
    base_url: str | None = None
    default_model: str | None = None
    description: str | None = None
    docs_url: str | None = None
    secret_placeholder: str | None = None
    supports_secret_ref: bool = True
    supports_custom_base_url: bool = True
    enabled: bool = True
    sort_order: int = 100


class AgentIntegrationConfigRequest(BaseModel):
    enabled: bool = True
    config: dict[str, str] = Field(default_factory=dict)


def _scope_rank(scope: str | None) -> int:
    if scope == "admin":
        return 2
    if scope == "operator":
        return 1
    return 0


async def _load_internal_system_agent(
    db: AsyncSession = Depends(get_db_session),
    service_agent_id: str | None = Header(default=None, alias="X-HermesHQ-Agent-ID"),
    service_agent_token: str | None = Header(default=None, alias="X-HermesHQ-Agent-Token"),
) -> Agent:
    if not service_agent_id or not service_agent_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing agent credentials")
    expected = create_agent_service_token(service_agent_id)
    if service_agent_token != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid agent credentials")
    agent = await db.get(Agent, service_agent_id)
    if not agent or agent.is_archived:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown agent")
    return agent


async def _get_control_agent(
    current_agent: Agent = Depends(_load_internal_system_agent),
) -> Agent:
    if not current_agent.is_system_agent:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="System agent access required")
    if _scope_rank(current_agent.system_scope) < 1:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operator scope required")
    return current_agent


async def _require_admin_scope(current_agent: Agent = Depends(_get_control_agent)) -> Agent:
    if _scope_rank(current_agent.system_scope) < 2:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin scope required")
    return current_agent


async def _load_admin_proxy(db: AsyncSession) -> User:
    result = await db.execute(
        select(User)
        .where(User.role == "admin", User.is_active.is_(True))
        .order_by(User.created_at.asc())
    )
    admin_user = result.scalars().first()
    if not admin_user:
        raise HTTPException(status_code=503, detail="No active admin user is available")
    return admin_user


async def _log_control_action(
    db: AsyncSession,
    current_agent: Agent,
    *,
    event_type: str,
    message: str,
    details: dict[str, Any] | None = None,
    target_agent_id: str | None = None,
) -> None:
    db.add(
        ActivityLog(
            agent_id=target_agent_id or current_agent.id,
            event_type=event_type,
            severity="info",
            message=message,
            details={
                "system_agent_id": current_agent.id,
                "system_agent_slug": current_agent.slug,
                "system_scope": current_agent.system_scope,
                **(details or {}),
            },
        )
    )
    await db.commit()


@router.get("/agents", response_model=list[AgentRead])
async def control_list_agents(
    request: Request,
    include_archived: bool = False,
    _: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> list[AgentRead]:
    admin_user = await _load_admin_proxy(db)
    return await agents_router.list_agents(request, include_archived, admin_user, db)


@router.post("/agents", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
async def control_create_agent(
    payload: AgentCreate,
    request: Request,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    admin_user = await _load_admin_proxy(db)
    created = await agents_router.create_agent(payload, request, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.agent.created",
        message=f"Created agent {created.slug}",
        details={"created_agent_id": created.id, "created_agent_slug": created.slug},
        target_agent_id=created.id,
    )
    return created


@router.put("/agents/{agent_id}", response_model=AgentRead)
async def control_update_agent(
    agent_id: str,
    payload: AgentUpdate,
    request: Request,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    admin_user = await _load_admin_proxy(db)
    updated = await agents_router.update_agent(agent_id, payload, request, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.agent.updated",
        message=f"Updated agent {updated.slug}",
        details={"updated_agent_id": updated.id, "updated_agent_slug": updated.slug},
        target_agent_id=updated.id,
    )
    return updated


@router.post("/agents/{agent_id}/archive", response_model=AgentRead)
async def control_archive_agent(
    agent_id: str,
    payload: ArchiveAgentRequest,
    request: Request,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    if agent_id == current_agent.id:
        raise HTTPException(status_code=400, detail="HQ Operator cannot archive itself")
    admin_user = await _load_admin_proxy(db)
    await agents_router.delete_agent(agent_id, request, admin_user, db)
    archived = await db.get(Agent, agent_id)
    if not archived:
        raise HTTPException(status_code=404, detail="Agent not found after archive")
    if payload.reason is not None:
        archived.archive_reason = payload.reason.strip() or None
    await db.commit()
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    agent = result.scalar_one()
    serialized = agents_router._serialize_agent(request, agent)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.agent.archived",
        message=f"Archived agent {serialized.slug}",
        details={"archived_agent_id": serialized.id, "reason": payload.reason},
        target_agent_id=serialized.id,
    )
    return serialized


@router.post("/agents/{agent_id}/runtime/{action}", response_model=AgentRead)
async def control_agent_runtime(
    agent_id: str,
    action: str,
    request: Request,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    admin_user = await _load_admin_proxy(db)
    if action == "start":
        result = await agents_router.start_agent(agent_id, request, admin_user, db)
    elif action == "stop":
        result = await agents_router.stop_agent(agent_id, request, admin_user, db)
    elif action == "restart":
        result = await agents_router.restart_agent(agent_id, request, admin_user, db)
    else:
        raise HTTPException(status_code=400, detail="Unsupported runtime action")
    await _log_control_action(
        db,
        current_agent,
        event_type=f"hq_control.agent.{action}",
        message=f"{action.title()} agent {result.slug}",
        details={"agent_id": result.id, "action": action},
        target_agent_id=result.id,
    )
    return result


@router.get("/users", response_model=list[UserManagedRead])
async def control_list_users(
    request: Request,
    _: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> list[UserManagedRead]:
    admin_user = await _load_admin_proxy(db)
    return await users_router.list_users(request, admin_user, db)


@router.post("/users", response_model=UserManagedRead, status_code=status.HTTP_201_CREATED)
async def control_create_user(
    payload: UserCreate,
    request: Request,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> UserManagedRead:
    admin_user = await _load_admin_proxy(db)
    created = await users_router.create_user(payload, request, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.user.created",
        message=f"Created user {created.username}",
        details={"user_id": created.id, "username": created.username},
    )
    return created


@router.put("/users/{user_id}", response_model=UserManagedRead)
async def control_update_user(
    user_id: str,
    payload: UserUpdate,
    request: Request,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> UserManagedRead:
    admin_user = await _load_admin_proxy(db)
    updated = await users_router.update_user(user_id, payload, request, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.user.updated",
        message=f"Updated user {updated.username}",
        details={"user_id": updated.id, "username": updated.username},
    )
    return updated


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def control_delete_user(
    user_id: str,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    admin_user = await _load_admin_proxy(db)
    user = await db.get(User, user_id)
    username = user.username if user else user_id
    await users_router.delete_user(user_id, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.user.deleted",
        message=f"Deleted user {username}",
        details={"user_id": user_id, "username": username},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/providers", response_model=list[ProviderRead])
async def control_list_providers(
    _: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> list[ProviderRead]:
    admin_user = await _load_admin_proxy(db)
    return await providers_router.list_providers(admin_user, db)


@router.post("/providers", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
async def control_create_provider(
    payload: ProviderCreateRequest,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> ProviderRead:
    existing = await db.get(ProviderDefinition, payload.slug)
    if existing:
        raise HTTPException(status_code=409, detail="Provider already exists")
    provider = ProviderDefinition(**payload.model_dump())
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.provider.created",
        message=f"Created provider {provider.slug}",
        details={"provider_slug": provider.slug},
    )
    return ProviderRead.model_validate(provider)


@router.put("/providers/{provider_slug}", response_model=ProviderRead)
async def control_update_provider(
    provider_slug: str,
    payload: ProviderUpdate,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> ProviderRead:
    admin_user = await _load_admin_proxy(db)
    updated = await providers_router.update_provider(provider_slug, payload, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.provider.updated",
        message=f"Updated provider {updated.slug}",
        details={"provider_slug": updated.slug},
    )
    return updated


@router.delete("/providers/{provider_slug}", status_code=status.HTTP_204_NO_CONTENT)
async def control_delete_provider(
    provider_slug: str,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    provider = await db.get(ProviderDefinition, provider_slug)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    await db.delete(provider)
    await db.commit()
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.provider.deleted",
        message=f"Deleted provider {provider_slug}",
        details={"provider_slug": provider_slug},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/secrets", response_model=list[SecretRead])
async def control_list_secrets(
    _: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> list[SecretRead]:
    admin_user = await _load_admin_proxy(db)
    return await secrets_router.list_secrets(admin_user, db)


@router.post("/secrets", response_model=SecretRead, status_code=status.HTTP_201_CREATED)
async def control_create_secret(
    payload: SecretCreate,
    request: Request,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> SecretRead:
    admin_user = await _load_admin_proxy(db)
    created = await secrets_router.create_secret(payload, request, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.secret.created",
        message=f"Created secret {created.name}",
        details={"secret_id": created.id, "secret_name": created.name},
    )
    return created


@router.put("/secrets/{secret_id}", response_model=SecretRead)
async def control_update_secret(
    secret_id: str,
    payload: SecretUpdate,
    request: Request,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> SecretRead:
    admin_user = await _load_admin_proxy(db)
    updated = await secrets_router.update_secret(secret_id, payload, request, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.secret.updated",
        message=f"Updated secret {updated.name}",
        details={"secret_id": updated.id, "secret_name": updated.name},
    )
    return updated


@router.delete("/secrets/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def control_delete_secret(
    secret_id: str,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    secret = await db.get(Secret, secret_id)
    secret_name = secret.name if secret else secret_id
    admin_user = await _load_admin_proxy(db)
    await secrets_router.delete_secret(secret_id, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.secret.deleted",
        message=f"Deleted secret {secret_name}",
        details={"secret_id": secret_id, "secret_name": secret_name},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/integrations", response_model=list[ManagedIntegrationRead])
async def control_list_integrations(
    _: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> list[ManagedIntegrationRead]:
    admin_user = await _load_admin_proxy(db)
    return await integration_packages_router.list_integration_packages(admin_user, db)


@router.post("/integrations/{slug}/install", response_model=ManagedIntegrationRead)
async def control_install_integration(
    slug: str,
    request: Request,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> ManagedIntegrationRead:
    admin_user = await _load_admin_proxy(db)
    result = await integration_packages_router.install_integration_package(slug, request, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.integration.installed",
        message=f"Installed integration {slug}",
        details={"integration_slug": slug},
    )
    return result


@router.post("/integrations/{slug}/uninstall", status_code=status.HTTP_204_NO_CONTENT)
async def control_uninstall_integration(
    slug: str,
    request: Request,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    admin_user = await _load_admin_proxy(db)
    await integration_packages_router.uninstall_integration_package(slug, request, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.integration.uninstalled",
        message=f"Uninstalled integration {slug}",
        details={"integration_slug": slug},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/integration-drafts", response_model=list[IntegrationDraftRead])
async def control_list_integration_drafts(
    _: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> list[IntegrationDraftRead]:
    admin_user = await _load_admin_proxy(db)
    return await integration_factory_router.list_integration_drafts(admin_user, db)


@router.post("/integration-drafts", response_model=IntegrationDraftRead, status_code=status.HTTP_201_CREATED)
async def control_create_integration_draft(
    payload: IntegrationDraftCreate,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftRead:
    admin_user = await _load_admin_proxy(db)
    created = await integration_factory_router.create_integration_draft(payload, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.integration_draft.created",
        message=f"Created integration draft {created.slug}",
        details={"draft_id": created.id, "draft_slug": created.slug},
    )
    return created


@router.get("/integration-drafts/{draft_id}", response_model=IntegrationDraftRead)
async def control_get_integration_draft(
    draft_id: str,
    _: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftRead:
    admin_user = await _load_admin_proxy(db)
    return await integration_factory_router.get_integration_draft(draft_id, admin_user, db)


@router.put("/integration-drafts/{draft_id}", response_model=IntegrationDraftRead)
async def control_update_integration_draft(
    draft_id: str,
    payload: IntegrationDraftUpdate,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftRead:
    admin_user = await _load_admin_proxy(db)
    updated = await integration_factory_router.update_integration_draft(draft_id, payload, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.integration_draft.updated",
        message=f"Updated integration draft {updated.slug}",
        details={"draft_id": updated.id, "draft_slug": updated.slug},
    )
    return updated


@router.delete("/integration-drafts/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def control_delete_integration_draft(
    draft_id: str,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    admin_user = await _load_admin_proxy(db)
    draft = await integration_factory_router.get_integration_draft(draft_id, admin_user, db)
    await integration_factory_router.delete_integration_draft(draft_id, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.integration_draft.deleted",
        message=f"Deleted integration draft {draft.slug}",
        details={"draft_id": draft.id, "draft_slug": draft.slug},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/integration-drafts/{draft_id}/file", response_model=IntegrationDraftFileContentRead)
async def control_get_integration_draft_file(
    draft_id: str,
    path: str,
    _: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftFileContentRead:
    admin_user = await _load_admin_proxy(db)
    return await integration_factory_router.get_integration_draft_file(draft_id, path, admin_user, db)


@router.put("/integration-drafts/{draft_id}/file", response_model=IntegrationDraftRead)
async def control_put_integration_draft_file(
    draft_id: str,
    path: str,
    payload: IntegrationDraftFileUpdate,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftRead:
    admin_user = await _load_admin_proxy(db)
    updated = await integration_factory_router.put_integration_draft_file(draft_id, payload, path, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.integration_draft.file_updated",
        message=f"Updated {path} in integration draft {updated.slug}",
        details={"draft_id": updated.id, "draft_slug": updated.slug, "path": path},
    )
    return updated


@router.delete("/integration-drafts/{draft_id}/file", response_model=IntegrationDraftRead)
async def control_delete_integration_draft_file(
    draft_id: str,
    path: str,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftRead:
    admin_user = await _load_admin_proxy(db)
    updated = await integration_factory_router.delete_integration_draft_file(draft_id, path, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.integration_draft.file_deleted",
        message=f"Deleted {path} from integration draft {updated.slug}",
        details={"draft_id": updated.id, "draft_slug": updated.slug, "path": path},
    )
    return updated


@router.post("/integration-drafts/{draft_id}/validate", response_model=IntegrationDraftValidationRead)
async def control_validate_integration_draft(
    draft_id: str,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftValidationRead:
    admin_user = await _load_admin_proxy(db)
    draft = await integration_factory_router.get_integration_draft(draft_id, admin_user, db)
    validation = await integration_factory_router.validate_integration_draft(draft_id, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.integration_draft.validated",
        message=f"Validated integration draft {draft.slug}",
        details={"draft_id": draft.id, "draft_slug": draft.slug, "valid": validation.valid},
    )
    return validation


@router.post("/integration-drafts/{draft_id}/publish", response_model=IntegrationDraftPublishRead)
async def control_publish_integration_draft(
    draft_id: str,
    request: Request,
    current_agent: Agent = Depends(_require_admin_scope),
    db: AsyncSession = Depends(get_db_session),
) -> IntegrationDraftPublishRead:
    admin_user = await _load_admin_proxy(db)
    published = await integration_factory_router.publish_integration_draft(draft_id, request, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.integration_draft.published",
        message=f"Published integration draft {published.draft.slug}",
        details={
            "draft_id": published.draft.id,
            "draft_slug": published.draft.slug,
            "integration_slug": published.integration.get("slug"),
        },
    )
    return published


@router.put("/agents/{agent_id}/integrations/{integration_slug}", response_model=AgentRead)
async def control_configure_agent_integration(
    agent_id: str,
    integration_slug: str,
    payload: AgentIntegrationConfigRequest,
    request: Request,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> AgentRead:
    agent = await db.get(Agent, agent_id)
    if not agent or agent.is_archived:
        raise HTTPException(status_code=404, detail="Agent not found")
    enabled_slugs = await agents_router._load_enabled_integration_slugs(db)
    integration = get_managed_integration(integration_slug, enabled_slugs)
    if not integration:
        raise HTTPException(status_code=404, detail="Integration is not installed in this instance")

    configs = dict(agent.integration_configs or {})
    skills = list(agent.skills or [])
    skill_identifier = integration.get("skill_identifier")

    if payload.enabled:
        configs[integration_slug] = dict(payload.config or {})
        if skill_identifier and skill_identifier not in skills:
            skills.append(str(skill_identifier))
    else:
        configs.pop(integration_slug, None)
        if skill_identifier and skill_identifier in skills:
            skills = [item for item in skills if item != skill_identifier]

    agent.integration_configs = configs
    agent.skills = skills
    agents_router._sync_agent_integration_toolsets(agent, enabled_slugs)
    await db.commit()
    await request.app.state.installation_manager.sync_agent_installation(agent)
    result = await db.execute(select(Agent).options(selectinload(Agent.node)).where(Agent.id == agent_id))
    updated = result.scalar_one()
    serialized = agents_router._serialize_agent(request, updated)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.agent.integration.updated",
        message=f"{'Enabled' if payload.enabled else 'Disabled'} {integration_slug} for {serialized.slug}",
        details={
            "integration_slug": integration_slug,
            "agent_id": serialized.id,
            "enabled": payload.enabled,
        },
        target_agent_id=serialized.id,
    )
    return serialized


@router.post("/agents/{agent_id}/integrations/{integration_slug}/test", response_model=ManagedIntegrationTestResult)
async def control_test_agent_integration(
    agent_id: str,
    integration_slug: str,
    payload: AgentIntegrationConfigRequest,
    request: Request,
    _: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> ManagedIntegrationTestResult:
    admin_user = await _load_admin_proxy(db)
    return await agents_router.test_agent_integration(
        agent_id,
        integration_slug,
        ManagedIntegrationTestRequest(config=payload.config),
        request,
        admin_user,
        db,
    )


@router.get("/scheduled-tasks", response_model=list[ScheduledTaskRead])
async def control_list_scheduled_tasks(
    _: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> list[ScheduledTaskRead]:
    admin_user = await _load_admin_proxy(db)
    return await scheduled_tasks_router.list_scheduled_tasks(admin_user, db)


@router.post("/scheduled-tasks", response_model=ScheduledTaskRead)
async def control_create_scheduled_task(
    payload: ScheduledTaskCreate,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledTaskRead:
    admin_user = await _load_admin_proxy(db)
    created = await scheduled_tasks_router.create_scheduled_task(payload, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.schedule.created",
        message=f"Created schedule {created.name}",
        details={"schedule_id": created.id, "agent_id": created.agent_id},
        target_agent_id=created.agent_id,
    )
    return created


@router.put("/scheduled-tasks/{scheduled_task_id}", response_model=ScheduledTaskRead)
async def control_update_scheduled_task(
    scheduled_task_id: str,
    payload: ScheduledTaskUpdate,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> ScheduledTaskRead:
    admin_user = await _load_admin_proxy(db)
    updated = await scheduled_tasks_router.update_scheduled_task(scheduled_task_id, payload, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.schedule.updated",
        message=f"Updated schedule {updated.name}",
        details={"schedule_id": updated.id, "agent_id": updated.agent_id},
        target_agent_id=updated.agent_id,
    )
    return updated


@router.delete("/scheduled-tasks/{scheduled_task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def control_delete_scheduled_task(
    scheduled_task_id: str,
    current_agent: Agent = Depends(_get_control_agent),
    db: AsyncSession = Depends(get_db_session),
) -> Response:
    admin_user = await _load_admin_proxy(db)
    schedule = await db.get(ScheduledTask, scheduled_task_id)
    target_agent_id = schedule.agent_id if schedule else None
    schedule_name = schedule.name if schedule else scheduled_task_id
    await scheduled_tasks_router.delete_scheduled_task(scheduled_task_id, admin_user, db)
    await _log_control_action(
        db,
        current_agent,
        event_type="hq_control.schedule.deleted",
        message=f"Deleted schedule {schedule_name}",
        details={"schedule_id": scheduled_task_id, "agent_id": target_agent_id},
        target_agent_id=target_agent_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
