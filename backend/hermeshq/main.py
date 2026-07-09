import asyncio
import base64
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from hermeshq.config import get_settings
from hermeshq.core.events import EventBroker, EventSubscription
from hermeshq.core.security import get_accessible_agent_ids, get_websocket_user, hash_password, is_admin
from hermeshq.database import AsyncSessionLocal, init_database
from hermeshq.models import ActivityLog, Agent, AppSettings, Node, ProviderDefinition, TerminalSession, User
from hermeshq.routers import (
    agents,
    attachments,
    audit,
    auth,
    backup,
    comms,
    dashboard,
    hermes_versions,
    integration_factory,
    integration_packages,
    internal_agents,
    internal_control,
    logs,
    m365,
    managed_integrations,
    mcp_access,
    mcp_server,
    messaging_channels,
    nodes,
    oidc_admin,
    providers,
    runtime_ledger,
    runtime_profiles,
    scheduled_tasks,
    secrets,
    skills,
    tasks,
    templates,
    terminal_sessions,
    users,
    voice,
    webhooks,
)
from hermeshq.routers import agent_builder
from hermeshq.routers import settings as settings_router
from hermeshq.schemas.common import HealthResponse
from hermeshq.services.agent_identity import derive_agent_identity
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.comms_router import CommsRouter
from hermeshq.services.enterprise_gateway_manager import EnterpriseGatewayManager
from hermeshq.services.gateway_supervisor import GatewaySupervisor
from hermeshq.services.hermes_installation import HermesInstallationManager
from hermeshq.services.hermes_runtime import HermesRuntime
from hermeshq.services.hermes_version_manager import HermesVersionManager
from hermeshq.services.instance_backup import InstanceBackupService
from hermeshq.services.provider_catalog import BUILTIN_PROVIDERS, normalize_runtime_provider, seed_provider_defaults
from hermeshq.services.pty_manager import PTYManager
from hermeshq.services.runtime_profiles import normalize_runtime_profile_slug, terminal_allowed_for_profile, STANDARD_ENABLED_TOOLSETS
from hermeshq.services.scheduler import SchedulerService
from hermeshq.services.secret_vault import SecretVault
from hermeshq.services.workspace_manager import WorkspaceManager
from hermeshq.versioning import get_app_version

logger = logging.getLogger(__name__)

settings = get_settings()
DEFAULT_ENABLED_INTEGRATION_PACKAGES = (
    "voice-edge",
    "voice-local",
)


async def bootstrap_defaults() -> None:
    import secrets as _secrets

    # Generate random admin password if not set
    admin_password = settings.admin_password
    if not admin_password or not admin_password.strip():
        admin_password = _secrets.token_urlsafe(16)
        logger.warning(
            "⚠️ ADMIN_PASSWORD was not set — auto-generated a secure password. "
            "Set ADMIN_PASSWORD in your environment to control this value."
        )

    async with AsyncSessionLocal() as session:
        user_result = await session.execute(select(User).where(User.username == settings.admin_username))
        admin_user = user_result.scalar_one_or_none()
        if not admin_user:
            session.add(
                User(
                    username=settings.admin_username,
                    display_name=settings.admin_display_name,
                    password_hash=hash_password(admin_password),
                    role="admin",
                    is_active=True,
                )
            )
        else:
            admin_user.role = "admin"
            admin_user.is_active = True
        node_result = await session.execute(select(Node).where(Node.name == "Local Runtime"))
        if not node_result.scalar_one_or_none():
            session.add(
                Node(
                    name="Local Runtime",
                    hostname="localhost",
                    node_type="local",
                    status="online",
                    system_info={"runtime": "local", "mode": "strict"},
                )
            )
        settings_row = await session.get(AppSettings, "default")
        if not settings_row:
            settings_row = AppSettings(id="default")
            session.add(settings_row)
        else:
            if settings_row.default_hermes_version == "bundled":
                settings_row.default_hermes_version = None
            normalized_default_provider = normalize_runtime_provider(settings_row.default_provider)
            if normalized_default_provider != settings_row.default_provider:
                settings_row.default_provider = normalized_default_provider
        enabled_packages = [
            slug
            for slug in (settings_row.enabled_integration_packages or [])
            if isinstance(slug, str) and slug.strip()
        ]
        for slug in DEFAULT_ENABLED_INTEGRATION_PACKAGES:
            if slug not in enabled_packages:
                enabled_packages.append(slug)
        settings_row.enabled_integration_packages = enabled_packages
        for payload in BUILTIN_PROVIDERS:
            provider = await session.get(ProviderDefinition, payload["slug"])
            if not provider:
                session.add(ProviderDefinition(**payload))
            else:
                seed_provider_defaults(provider, payload)
                if provider.slug == "kimi-coding":
                    normalized_kimi_url = (provider.base_url or "").strip().rstrip("/")
                    if normalized_kimi_url in {
                        "https://api.kimi.com/coding",
                        "https://api.moonshot.ai/v1",
                    }:
                        provider.base_url = "https://api.kimi.com/coding/v1"
                    if (provider.default_model or "").strip() in {"kimi-for-coding", "kimi-k2-turbo-preview"}:
                        provider.default_model = "kimi-k2.5"
        obsolete_openai_oauth = await session.get(ProviderDefinition, "openai-oauth")
        if obsolete_openai_oauth:
            await session.delete(obsolete_openai_oauth)
        agent_result = await session.execute(select(Agent).order_by(Agent.created_at.asc()))
        seen_slugs: set[str] = set()
        for agent in agent_result.scalars().all():
            resolved_friendly, resolved_name, resolved_slug = derive_agent_identity(
                friendly_name=agent.friendly_name,
                name=agent.name,
                slug=agent.slug,
            )
            if agent.workspace_path:
                workspace_path = Path(agent.workspace_path)
                if not workspace_path.is_absolute():
                    agent.workspace_path = str((settings.workspaces_root.parent / workspace_path).resolve())
            candidate_slug = resolved_slug
            suffix = 2
            while candidate_slug in seen_slugs:
                candidate_slug = f"{resolved_slug}-{suffix}"
                suffix += 1
            seen_slugs.add(candidate_slug)
            agent.friendly_name = resolved_friendly
            agent.name = resolved_name
            agent.slug = candidate_slug
            normalized_provider = normalize_runtime_provider(agent.provider)
            if normalized_provider != agent.provider:
                agent.provider = normalized_provider
            agent.runtime_profile = normalize_runtime_profile_slug(agent.runtime_profile)

            # ── Inherit new standard toolsets ──────────────────────────
            # Any toolset added to STANDARD_ENABLED_TOOLSETS is automatically
            # inherited by existing agents on upgrade. This is additive only —
            # it never removes toolsets an agent already has, and respects
            # any toolsets the agent has explicitly disabled.
            current_toolsets = list(agent.enabled_toolsets or [])
            disabled_set = set(agent.disabled_toolsets or [])
            changed = False
            for ts in STANDARD_ENABLED_TOOLSETS:
                if ts not in current_toolsets and ts not in disabled_set:
                    current_toolsets.append(ts)
                    changed = True
            if changed:
                agent.enabled_toolsets = current_toolsets
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_database()
    await bootstrap_defaults()
    app.state.event_broker = EventBroker()
    app.state.workspace_manager = WorkspaceManager(settings.workspaces_root)
    secret_vault_seed = settings.fernet_key or settings.jwt_secret
    if not settings.fernet_key:
        logger.warning(
            "⚠️ FERNET_KEY not set — SecretVault is using jwt_secret as fallback. "
            "Generate a key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    app.state.secret_vault = SecretVault(secret_vault_seed)
    app.state.hermes_version_manager = HermesVersionManager(AsyncSessionLocal)
    await app.state.hermes_version_manager.ensure_default_catalog_entries()
    app.state.instance_backup_service = InstanceBackupService(AsyncSessionLocal)
    app.state.installation_manager = HermesInstallationManager(
        AsyncSessionLocal,
        app.state.secret_vault,
        app.state.hermes_version_manager,
    )
    app.state.runtime = HermesRuntime(AsyncSessionLocal, app.state.secret_vault, app.state.installation_manager)
    app.state.supervisor = AgentSupervisor(
        AsyncSessionLocal,
        app.state.event_broker,
        app.state.runtime,
        app.state.secret_vault,
    )
    app.state.gateway_supervisor = GatewaySupervisor(
        AsyncSessionLocal,
        app.state.event_broker,
        app.state.installation_manager,
    )
    app.state.enterprise_gateways = EnterpriseGatewayManager(
        AsyncSessionLocal,
        app.state.supervisor,
        app.state.event_broker,
        app.state.secret_vault,
    )
    app.state.gateway_supervisor.set_enterprise_gateways(app.state.enterprise_gateways)
    # Expose individual gateway maps for webhook routing
    app.state.session_factory = AsyncSessionLocal
    app.state.google_chat_gateways = app.state.enterprise_gateways.google_chat_gateways
    app.state.kapso_gateways = app.state.enterprise_gateways.kapso_gateways
    app.state.comms_router = CommsRouter(AsyncSessionLocal, app.state.event_broker)
    async def log_terminal_activity(agent_id: str, event_type: str, message: str, details: dict) -> None:
        async with AsyncSessionLocal() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                return
            agent.last_activity = datetime.now(UTC)
            session_id = str(details.get("session_id") or "").strip()
            terminal_session = None
            if session_id:
                terminal_session = await session.get(TerminalSession, session_id)
                if event_type == "terminal.session.started":
                    terminal_session = terminal_session or TerminalSession(
                        id=session_id,
                        agent_id=agent_id,
                        node_id=agent.node_id,
                        mode=str(details.get("mode") or "hybrid"),
                        cwd=str(details.get("cwd") or ""),
                        command_json=list(details.get("command") or []),
                        status="open",
                        started_at=datetime.now(UTC),
                    )
                    session.add(terminal_session)
                elif terminal_session:
                    terminal_session.updated_at = datetime.now(UTC)
            if terminal_session:
                if event_type == "terminal.input":
                    terminal_session.input_transcript += f"{message}\n"
                    terminal_session.transcript_text += f"> {message}\n"
                elif event_type == "terminal.output":
                    terminal_session.output_transcript += f"{message}\n"
                    terminal_session.transcript_text += f"< {message}\n"
                elif event_type == "terminal.session.closed":
                    terminal_session.status = "closed"
                    terminal_session.ended_at = datetime.now(UTC)
                    exit_code = details.get("exit_code")
                    terminal_session.exit_code = int(exit_code) if isinstance(exit_code, int) else None
            session.add(
                ActivityLog(
                    agent_id=agent_id,
                    node_id=agent.node_id,
                    event_type=event_type,
                    severity="info",
                    message=message,
                    details=details,
                )
            )
            await session.commit()
        await app.state.event_broker.publish(
            {
                "type": "activity.created",
                "agent_id": agent_id,
                "event_type": event_type,
                "message": message,
            }
        )

    app.state.pty_manager = PTYManager(settings.pty_shell, audit_callback=log_terminal_activity)
    app.state.supervisor.pty_manager = app.state.pty_manager
    app.state.scheduler = SchedulerService(AsyncSessionLocal, app.state.supervisor.submit_task)
    await app.state.supervisor.bootstrap_runtime()
    await app.state.scheduler.start()
    app.state.gateway_bootstrap_task = asyncio.create_task(app.state.gateway_supervisor.bootstrap_gateways())
    app.state.enterprise_bootstrap_task = asyncio.create_task(app.state.enterprise_gateways.bootstrap())

    async def _periodic_cleanup() -> None:
        from hermeshq.routers.mcp_server import _rate_limiter, _analytics
        while True:
            await asyncio.sleep(60)
            _rate_limiter.cleanup()
            _analytics.evict_stale()

    app.state._cleanup_task = asyncio.create_task(_periodic_cleanup())

    yield

    app.state._cleanup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await app.state._cleanup_task
    gateway_bootstrap_task = getattr(app.state, "gateway_bootstrap_task", None)
    if gateway_bootstrap_task:
        gateway_bootstrap_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gateway_bootstrap_task
    enterprise_bootstrap_task = getattr(app.state, "enterprise_bootstrap_task", None)
    if enterprise_bootstrap_task:
        enterprise_bootstrap_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await enterprise_bootstrap_task
    await app.state.scheduler.stop()
    await app.state.gateway_supervisor.shutdown()
    await app.state.enterprise_gateways.shutdown()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# Request logging middleware (added first = executes last, so it logs the final response)
from hermeshq.core.request_logging import RequestLoggingMiddleware
from hermeshq.core.structured_errors import StructuredErrorMiddleware

app.add_middleware(StructuredErrorMiddleware)
app.add_middleware(RequestLoggingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With", "Accept", "Origin"],
)

app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(nodes.router, prefix=settings.api_prefix)
app.include_router(providers.router, prefix=settings.api_prefix)
app.include_router(hermes_versions.router, prefix=settings.api_prefix)
app.include_router(runtime_profiles.router, prefix=settings.api_prefix)
app.include_router(integration_factory.router, prefix=settings.api_prefix)
app.include_router(integration_packages.router, prefix=settings.api_prefix)
app.include_router(managed_integrations.router, prefix=settings.api_prefix)
app.include_router(mcp_access.router, prefix=settings.api_prefix)
app.include_router(agents.router, prefix=settings.api_prefix)
app.include_router(tasks.router, prefix=settings.api_prefix)
app.include_router(runtime_ledger.router, prefix=settings.api_prefix)
app.include_router(dashboard.router, prefix=settings.api_prefix)
app.include_router(comms.router, prefix=settings.api_prefix)
app.include_router(internal_agents.router, prefix=settings.api_prefix)
app.include_router(internal_control.router, prefix=settings.api_prefix)
app.include_router(secrets.router, prefix=settings.api_prefix)
app.include_router(settings_router.router, prefix=settings.api_prefix)
app.include_router(backup.router, prefix=settings.api_prefix)
app.include_router(skills.router, prefix=settings.api_prefix)
app.include_router(messaging_channels.router, prefix=settings.api_prefix)
app.include_router(templates.router, prefix=settings.api_prefix)
app.include_router(logs.router, prefix=settings.api_prefix)
app.include_router(terminal_sessions.router, prefix=settings.api_prefix)
app.include_router(scheduled_tasks.router, prefix=settings.api_prefix)
app.include_router(oidc_admin.router, prefix=settings.api_prefix)
app.include_router(users.router, prefix=settings.api_prefix)
app.include_router(audit.router, prefix=settings.api_prefix)
app.include_router(mcp_server.router)
app.include_router(webhooks.router)
app.include_router(attachments.router, prefix=settings.api_prefix)
app.include_router(m365.router, prefix=settings.api_prefix)
app.include_router(voice.router, prefix=settings.api_prefix)
app.include_router(agent_builder.router, prefix=settings.api_prefix)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    db_ok = True
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(select(1))
    except OSError:
        db_ok = False
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        timestamp=datetime.now(UTC),
        version=get_app_version(),
    )


@app.websocket("/ws/stream")
async def stream(websocket: WebSocket) -> None:
    broker: EventBroker = app.state.event_broker

    # --- Authentication: support both query-param (legacy) and first-message auth ---
    token: str | None = websocket.query_params.get("token")

    if not token:
        # Accept the connection provisionally and wait for an auth message.
        await websocket.accept()
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            payload = json.loads(raw)
            if payload.get("type") == "auth":
                token = payload.get("token")
        except (TimeoutError, json.JSONDecodeError, KeyError):
            await websocket.close(code=4401)
            return

    from hermeshq.core.security import decode_access_token_claims
    claims = decode_access_token_claims(token or "")
    if not claims or not claims.get("sub"):
        await websocket.close(code=4401)
        return

    user_id: str = claims["sub"]
    user_role: str = claims.get("role", "user")
    # user_is_admin derived but handled via is_admin() elsewhere

    # Use agent_ids from token if available (no DB needed), otherwise fall back to DB query
    token_agent_ids: list[str] | None = claims.get("agent_ids")
    if token_agent_ids is not None:
        accessible_agent_ids = token_agent_ids
    else:
        async with AsyncSessionLocal() as session:
            from hermeshq.core.security import get_user_by_subject
            user = await get_user_by_subject(session, user_id, claims.get("sub_kind"))
            if not user or not user.is_active:
                await websocket.close(code=4401)
                return
            accessible_agent_ids = await get_accessible_agent_ids(session, user)

    # Build a lightweight user-like object for the broker subscription
    user = SimpleNamespace(id=user_id, role=user_role, is_active=True)

    # If we already accepted (message-based auth), don't accept again.
    if websocket.client_state.name == "CONNECTED":
        broker._connections[websocket] = EventSubscription(
            websocket=websocket,
            is_admin=is_admin(user),
            agent_ids=set(accessible_agent_ids),
        )
    else:
        await broker.connect(websocket, is_admin=is_admin(user), agent_ids=accessible_agent_ids)

    # Handle pong responses for heartbeat
    try:
        while True:
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
                if data.get("type") == "pong":
                    continue
            except (json.JSONDecodeError, KeyError, TypeError):
                logger.debug("WebSocket received non-JSON message", exc_info=True)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001  # WebSocket lifecycle — any error closes the socket
        logger.warning("WebSocket stream unexpected error: %s", exc)
    finally:
        # Single cleanup point for all exit paths
        broker.disconnect(websocket)


@app.websocket("/ws/pty/{agent_id}")
async def pty_stream(websocket: WebSocket, agent_id: str) -> None:
    mode = "hybrid"
    async with AsyncSessionLocal() as session:
        user = await get_websocket_user(websocket, session)
        if not user:
            await websocket.close(code=4401)
            return
        agent = await session.get(Agent, agent_id)
        if not agent:
            await websocket.close(code=4404)
            return
        accessible_agent_ids = await get_accessible_agent_ids(session, user)
        if not is_admin(user) and agent_id not in accessible_agent_ids:
            await websocket.close(code=4403)
            return
        mode = agent.run_mode
        if mode == "headless":
            await websocket.close(code=4400, reason="Agent is headless")
            return
        if not terminal_allowed_for_profile(agent.runtime_profile):
            await websocket.close(code=4400, reason="Terminal is disabled for this runtime profile")
            return
        await app.state.installation_manager.sync_agent_installation(agent)
        cwd = str(app.state.installation_manager.resolve_workspace_path(agent.workspace_path))
        env = await app.state.installation_manager.build_process_env(agent)
        runtime_selection = await app.state.installation_manager.resolve_hermes_runtime(agent)
        command = [runtime_selection.hermes_bin]
    pty_session = await app.state.pty_manager.create_session(agent_id, mode, cwd, command=command, env=env)
    await app.state.pty_manager.attach(pty_session, websocket)
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "input":
                await app.state.pty_manager.write_input(
                    agent_id,
                    base64.b64decode(message.get("data", "")),
                )
            elif message.get("type") == "resize":
                await app.state.pty_manager.resize(
                    agent_id,
                    int(message.get("cols", pty_session.cols)),
                    int(message.get("rows", pty_session.rows)),
                )
            elif message.get("type") == "detach":
                break
    except WebSocketDisconnect:
        pass
    finally:
        await app.state.pty_manager.detach(pty_session, websocket)
        if pty_session.mode == "hybrid" and not pty_session.connections:
            await app.state.pty_manager.destroy_session(agent_id)
