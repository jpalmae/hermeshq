from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user, is_admin, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.user import User
from hermeshq.models.user_m365_token import UserM365Token
from hermeshq.services.m365_oauth import (
    AVAILABLE_SCOPES,
    M365ConfigError,
    M365TokenError,
    complete_device_flow,
    get_instance_m365_config,
    get_valid_token,
    initiate_device_flow,
    revoke_user_token,
)

router = APIRouter(prefix="/m365", tags=["m365"])

# Almacenamiento en memoria para flows pendientes (user_id → flow_state)
# En producción con múltiples workers se debería usar Redis/cache compartido,
# pero para una instancia single-server es suficiente.
_pending_flows: dict[str, dict] = {}


# ─── Schemas ────────────────────────────────────────────────────────────────


class M365AppConfigRead(BaseModel):
    client_id: str | None
    tenant_id: str | None
    enabled_scopes: list[str]
    available_scopes: dict[str, str]
    configured: bool


class M365AppConfigUpdate(BaseModel):
    client_id: str | None = None
    tenant_id: str | None = None
    enabled_scopes: list[str] | None = None


class M365UserTokenRead(BaseModel):
    connected: bool
    account_email: str | None = None
    account_name: str | None = None
    scopes: list[str] = []
    expires_at: datetime | None = None
    revoked: bool = False


class M365ConnectFlowRead(BaseModel):
    verification_uri: str
    user_code: str
    expires_in: int


class M365ConnectStatusRead(BaseModel):
    status: str
    account_email: str | None = None
    account_name: str | None = None


class M365AdminTokenRead(BaseModel):
    id: str
    user_id: str
    account_email: str | None = None
    account_name: str | None = None
    scopes: list[str] = []
    expires_at: datetime | None = None
    revoked: bool
    created_at: datetime | None = None


class AgentM365ScopesRead(BaseModel):
    allowed_scopes: list[str] | None = None
    user_scopes: list[str] = []
    available_scopes: dict[str, str] = {}
    sharepoint_site_url: str | None = None  # Optional SharePoint site URL for this agent


# ─── Admin: configuración de la instancia ───────────────────────────────────


@router.get("/config", response_model=M365AppConfigRead)
async def get_m365_config(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> M365AppConfigRead:
    config = await get_instance_m365_config(db)
    if config:
        return M365AppConfigRead(
            client_id=config["client_id"],
            tenant_id=config["tenant_id"],
            enabled_scopes=config["enabled_scopes"],
            available_scopes=AVAILABLE_SCOPES,
            configured=True,
        )
    settings = await db.get(AppSettings, "default")
    return M365AppConfigRead(
        client_id=settings.m365_client_id if settings else None,
        tenant_id=settings.m365_tenant_id if settings else None,
        enabled_scopes=[],
        available_scopes=AVAILABLE_SCOPES,
        configured=False,
    )


@router.put("/config", response_model=M365AppConfigRead)
async def update_m365_config(
    payload: M365AppConfigUpdate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> M365AppConfigRead:
    settings = await db.get(AppSettings, "default")
    if not settings:
        settings = AppSettings(id="default")
        db.add(settings)

    if payload.client_id is not None:
        settings.m365_client_id = payload.client_id.strip() or None
    if payload.tenant_id is not None:
        settings.m365_tenant_id = payload.tenant_id.strip() or None
    if payload.enabled_scopes is not None:
        valid = [s for s in payload.enabled_scopes if s in AVAILABLE_SCOPES]
        settings.m365_enabled_scopes = valid

    await db.commit()
    await db.refresh(settings)

    configured = bool((settings.m365_client_id or "").strip() and (settings.m365_tenant_id or "").strip())
    return M365AppConfigRead(
        client_id=settings.m365_client_id,
        tenant_id=settings.m365_tenant_id,
        enabled_scopes=list(settings.m365_enabled_scopes or []),
        available_scopes=AVAILABLE_SCOPES,
        configured=configured,
    )


# ─── Admin: ver tokens de usuarios ──────────────────────────────────────────


@router.get("/admin/tokens", response_model=list[M365AdminTokenRead])
async def list_user_tokens(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
    offset: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=500, description="Max records to return"),
) -> list[dict]:
    result = await db.execute(
        select(UserM365Token).offset(offset).limit(limit).order_by(UserM365Token.created_at.desc())
    )
    tokens = result.scalars().all()
    return [
        {
            "id": t.id,
            "user_id": t.user_id,
            "account_email": t.account_email,
            "account_name": t.account_name,
            "scopes": t.scopes.split() if t.scopes else [],
            "expires_at": t.expires_at,
            "revoked": t.revoked_at is not None,
            "created_at": t.created_at,
        }
        for t in tokens
    ]


@router.delete("/admin/tokens/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_revoke_user_token(
    user_id: str,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    await revoke_user_token(user_id, db)


# ─── Usuario: su propia cuenta M365 ─────────────────────────────────────────


@router.get("/me", response_model=M365UserTokenRead)
async def get_my_m365_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> M365UserTokenRead:
    result = await db.execute(select(UserM365Token).where(UserM365Token.user_id == current_user.id))
    token_record = result.scalar_one_or_none()
    if not token_record:
        return M365UserTokenRead(connected=False)
    return M365UserTokenRead(
        connected=token_record.revoked_at is None,
        account_email=token_record.account_email,
        account_name=token_record.account_name,
        scopes=token_record.scopes.split() if token_record.scopes else [],
        expires_at=token_record.expires_at,
        revoked=token_record.revoked_at is not None,
    )


@router.post("/me/connect", response_model=M365ConnectFlowRead)
async def start_connect(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> M365ConnectFlowRead:
    try:
        flow_state = await initiate_device_flow(db)
    except M365ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    _pending_flows[current_user.id] = flow_state
    return M365ConnectFlowRead(
        verification_uri=flow_state["verification_uri"],
        user_code=flow_state["user_code"],
        expires_in=flow_state["expires_in"],
    )


@router.get("/me/connect/status", response_model=M365ConnectStatusRead)
async def poll_connect_status(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> M365ConnectStatusRead:
    flow_state = _pending_flows.get(current_user.id)
    if not flow_state:
        raise HTTPException(status_code=404, detail="No pending authentication flow.")

    vault = request.app.state.secret_vault

    try:
        token_record = await complete_device_flow(
            flow_state=flow_state,
            vault=vault,
            db=db,
            user_id=current_user.id,
        )
        _pending_flows.pop(current_user.id, None)
        return M365ConnectStatusRead(
            status="connected",
            account_email=token_record.account_email,
            account_name=token_record.account_name,
        )
    except M365TokenError as exc:
        error_msg = str(exc)
        logger.warning("M365 poll error for user %s: %s", current_user.id, error_msg)
        if exc.error_code in ("authorization_pending", "authorization_declined"):
            return M365ConnectStatusRead(status="pending")
        _pending_flows.pop(current_user.id, None)
        raise HTTPException(status_code=400, detail=error_msg) from exc
    except Exception as exc:  # noqa: BLE001  # M365 auth — surface unexpected errors as 500
        logger.exception("M365 unexpected error for user %s", current_user.id)
        _pending_flows.pop(current_user.id, None)
        raise HTTPException(status_code=500, detail="Unexpected error during authentication.") from exc


@router.get("/me/agents/{agent_id}/scopes", response_model=AgentM365ScopesRead)
async def get_agent_m365_scopes(
    agent_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentM365ScopesRead:
    from hermeshq.core.security import ensure_agent_access
    from hermeshq.models.agent_assignment import AgentAssignment
    from hermeshq.services.m365_oauth import AVAILABLE_SCOPES

    # Authorization: only users who already have access to this agent (assigned, or
    # admin) may read/configure its M365 scopes. This endpoint used to auto-create an
    # AgentAssignment for any agent_id, which let any user grant themselves access to
    # an agent and was the entry point for cross-user M365 token theft.
    await ensure_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(AgentAssignment).where(
            AgentAssignment.user_id == current_user.id,
            AgentAssignment.agent_id == agent_id,
        )
    )
    assignment = result.scalar_one_or_none()
    token_result = await db.execute(select(UserM365Token).where(UserM365Token.user_id == current_user.id))
    token = token_result.scalar_one_or_none()
    user_scopes = token.scopes.split() if token and token.scopes else []
    return {
        "allowed_scopes": assignment.m365_allowed_scopes if assignment else None,
        "user_scopes": user_scopes,
        "available_scopes": {k: v for k, v in AVAILABLE_SCOPES.items() if k in user_scopes},
        "sharepoint_site_url": assignment.sharepoint_site_url if assignment else None,
    }


class AgentScopesUpdate(BaseModel):
    allowed_scopes: list[str] | None
    sharepoint_site_url: str | None = None  # Optional SharePoint site URL for this agent


# Mapping: which scopes activate which delegated integration
# Keys must match the actual Azure AD granted permissions
_SCOPE_TO_INTEGRATION: dict[str, str] = {
    "Mail.Read": "ms365-mail",
    "Mail.Send": "ms365-mail",
    "Calendars.Read": "ms365-calendar",
    "Calendars.ReadWrite": "ms365-calendar",
    "Files.Read.All": "sharepoint",
    "Chat.Read": "ms365-teams",
    "Chat.ReadWrite": "ms365-teams",
    "Team.ReadBasic.All": "ms365-teams",
}

# Companion skill each integration ships with (must match manifest skill_identifier)
_INTEGRATION_SKILL: dict[str, str] = {
    "ms365-mail": "local/ms365-mail",
    "ms365-calendar": "local/ms365-calendar",
    "sharepoint": "local/sharepoint",
    "ms365-teams": "local/ms365-teams",
}

# Plugin slug each integration ships with (must match manifest plugin_slug)
_INTEGRATION_PLUGIN: dict[str, str] = {
    "ms365-mail": "hermeshq_ms365_mail",
    "ms365-calendar": "hermeshq_ms365_calendar",
    "sharepoint": "hermeshq_sharepoint",
    "ms365-teams": "hermeshq_ms365_teams",
}


@router.put("/me/agents/{agent_id}/scopes", response_model=AgentM365ScopesRead)
async def update_agent_m365_scopes(
    agent_id: str,
    payload: AgentScopesUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentM365ScopesRead:
    from hermeshq.models.agent import Agent
    from hermeshq.models.agent_assignment import AgentAssignment

    result = await db.execute(
        select(AgentAssignment).where(
            AgentAssignment.user_id == current_user.id,
            AgentAssignment.agent_id == agent_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found.")
    assignment.m365_allowed_scopes = payload.allowed_scopes
    site_url = (payload.sharepoint_site_url or "").strip() or None
    assignment.sharepoint_site_url = site_url

    # Only admins may mutate shared agent properties (toolsets/skills/integration_configs).
    # Non-admin users configure only their own scopes and SharePoint site (per-assignment).
    agent = None
    if is_admin(current_user):
        agent = await db.get(Agent, agent_id)
        if agent:
            scopes = payload.allowed_scopes or []
            activated_integrations = {_SCOPE_TO_INTEGRATION[s] for s in scopes if s in _SCOPE_TO_INTEGRATION}
            current_configs = dict(agent.integration_configs or {})
            current_skills = list(agent.skills or [])
            current_toolsets = list(agent.enabled_toolsets or [])
            changed = False
            for integration_slug in activated_integrations:
                # 1. Enable in integration_configs
                if integration_slug not in current_configs:
                    current_configs[integration_slug] = {}
                    changed = True
                    logger.info("Auto-enabled integration '%s' for agent %s", integration_slug, agent_id)
                # 2. Add companion skill (provides SKILL.md context)
                skill_id = _INTEGRATION_SKILL.get(integration_slug)
                if skill_id and skill_id not in current_skills:
                    current_skills.append(skill_id)
                    changed = True
                    logger.info("Auto-added skill '%s' to agent %s", skill_id, agent_id)
                # 3. Add plugin to enabled_toolsets (provides actual tools)
                plugin_id = _INTEGRATION_PLUGIN.get(integration_slug)
                if plugin_id and plugin_id not in current_toolsets:
                    current_toolsets.append(plugin_id)
                    changed = True
                    logger.info("Auto-added toolset '%s' to agent %s", plugin_id, agent_id)

            if changed:
                agent.integration_configs = current_configs
                agent.skills = current_skills
                agent.enabled_toolsets = current_toolsets

    await db.commit()

    # Sync agent .env so HERMESHQ_SHAREPOINT_SITE_URL takes effect without manual restart
    if agent:
        try:
            await request.app.state.installation_manager.sync_agent_installation(agent)
        except Exception:
            logger.warning(
                "Failed to sync agent installation after M365 scopes update for agent %s", agent_id, exc_info=True
            )

    return {
        "allowed_scopes": assignment.m365_allowed_scopes,
        "sharepoint_site_url": assignment.sharepoint_site_url,
    }


@router.get("/agent-token")
async def get_agent_m365_token(
    request: Request,
    user_id: str,
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    """Called by managed integration plugins running inside an agent runtime.
    Validates agent credentials, verifies the agent is assigned to the user,
    checks allowed scopes, and returns a fresh access token."""
    import hmac as _hmac

    from hermeshq.core.security import create_agent_service_token
    from hermeshq.models.agent import Agent
    from hermeshq.models.agent_assignment import AgentAssignment

    agent_id = request.headers.get("X-HermesHQ-Agent-ID", "").strip()
    agent_token = request.headers.get("X-HermesHQ-Agent-Token", "").strip()
    if not agent_id or not agent_token:
        raise HTTPException(status_code=401, detail="Missing agent credentials")
    expected = create_agent_service_token(agent_id)
    if not _hmac.compare_digest(agent_token, expected):
        raise HTTPException(status_code=401, detail="Invalid agent credentials")
    agent = await db.get(Agent, agent_id)
    if not agent or agent.is_archived:
        raise HTTPException(status_code=401, detail="Unknown agent")

    assignment_result = await db.execute(
        select(AgentAssignment).where(
            AgentAssignment.agent_id == agent_id,
            AgentAssignment.user_id == user_id,
        )
    )
    assignment = assignment_result.scalar_one_or_none()
    if not assignment:
        raise HTTPException(status_code=403, detail="This agent is not assigned to this user.")

    vault = request.app.state.secret_vault
    try:
        access_token, _, granted_scopes = await get_valid_token(user_id, vault, db)
    except M365TokenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if not access_token:
        raise HTTPException(status_code=403, detail="User does not have an M365 account connected.")

    allowed = assignment.m365_allowed_scopes
    if allowed is not None:
        granted_scopes = [s for s in (granted_scopes or []) if s in allowed]

    return {
        "access_token": access_token,
        "scopes": granted_scopes,
        "sharepoint_site_url": assignment.sharepoint_site_url,
    }


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_my_m365(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    _pending_flows.pop(current_user.id, None)
    await revoke_user_token(current_user.id, db)
