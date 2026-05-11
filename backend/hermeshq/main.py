import asyncio
import base64
import contextlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from hermeshq.config import get_settings
from hermeshq.core.events import EventBroker, EventSubscription
from hermeshq.core.security import get_accessible_agent_ids, get_websocket_user, hash_password, is_admin
from hermeshq.database import AsyncSessionLocal, init_database
from hermeshq.models import ActivityLog, Agent, AppSettings, Node, ProviderDefinition, TerminalSession, User
from hermeshq.routers import agents, auth, backup, comms, dashboard, hermes_versions, integration_factory, integration_packages, internal_agents, internal_control, logs, managed_integrations, mcp_access, mcp_server, messaging_channels, nodes, providers, runtime_ledger, runtime_profiles, scheduled_tasks, secrets, settings as settings_router, skills, tasks, templates, terminal_sessions, users
from hermeshq.schemas.common import HealthResponse
from hermeshq.services.agent_identity import derive_agent_identity, slugify_agent_value
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.comms_router import CommsRouter
from hermeshq.services.hermes_installation import HermesInstallationManager
from hermeshq.services.hermes_runtime import HermesRuntime
from hermeshq.services.gateway_supervisor import GatewaySupervisor
from hermeshq.services.hermes_version_manager import HermesVersionManager
from hermeshq.services.instance_backup import InstanceBackupService
from hermeshq.services.pty_manager import PTYManager
from hermeshq.services.provider_catalog import BUILTIN_PROVIDERS, normalize_runtime_provider, seed_provider_defaults
from hermeshq.services.runtime_profiles import normalize_runtime_profile_slug, terminal_allowed_for_profile
from hermeshq.services.scheduler import SchedulerService
from hermeshq.services.secret_vault import SecretVault
from hermeshq.services.workspace_manager import WorkspaceManager
from hermeshq.versioning import get_app_version

settings = get_settings()
DEFAULT_ENABLED_INTEGRATION_PACKAGES = (
    "voice-edge",
    "voice-local",
)


async def bootstrap_defaults() -> None:
    async with AsyncSessionLocal() as session:
        user_result = await session.execute(select(User).where(User.username == settings.admin_username))
        admin_user = user_result.scalar_one_or_none()
        if not admin_user:
            session.add(
                User(
                    username=settings.admin_username,
                    display_name=settings.admin_display_name,
                    password_hash=hash_password(settings.admin_password),
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
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_database()
    await bootstrap_defaults()
    app.state.event_broker = EventBroker()
    app.state.workspace_manager = WorkspaceManager(settings.workspaces_root)
    app.state.secret_vault = SecretVault(settings.fernet_key or settings.jwt_secret)
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
    app.state.comms_router = CommsRouter(AsyncSessionLocal, app.state.event_broker)
    async def log_terminal_activity(agent_id: str, event_type: str, message: str, details: dict) -> None:
        async with AsyncSessionLocal() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                return
            agent.last_activity = datetime.now(timezone.utc)
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
                        started_at=datetime.now(timezone.utc),
                    )
                    session.add(terminal_session)
                elif terminal_session:
                    terminal_session.updated_at = datetime.now(timezone.utc)
            if terminal_session:
                if event_type == "terminal.input":
                    terminal_session.input_transcript += f"{message}\n"
                    terminal_session.transcript_text += f"> {message}\n"
                elif event_type == "terminal.output":
                    terminal_session.output_transcript += f"{message}\n"
                    terminal_session.transcript_text += f"< {message}\n"
                elif event_type == "terminal.session.closed":
                    terminal_session.status = "closed"
                    terminal_session.ended_at = datetime.now(timezone.utc)
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
    yield
    gateway_bootstrap_task = getattr(app.state, "gateway_bootstrap_task", None)
    if gateway_bootstrap_task:
        gateway_bootstrap_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gateway_bootstrap_task
    await app.state.scheduler.stop()
    await app.state.gateway_supervisor.shutdown()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
app.include_router(users.router, prefix=settings.api_prefix)
app.include_router(mcp_server.router)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", timestamp=datetime.now(timezone.utc), version=get_app_version())


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
        except Exception:
            await websocket.close(code=4401)
            return

    async with AsyncSessionLocal() as session:
        from hermeshq.core.security import decode_access_token_subject, get_user_by_subject
        subject, subject_kind = decode_access_token_subject(token or "")
        user = await get_user_by_subject(session, subject, subject_kind)
        if not user or not user.is_active:
            await websocket.close(code=4401)
            return
        accessible_agent_ids = await get_accessible_agent_ids(session, user)

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
            except Exception:
                pass
    except WebSocketDisconnect:
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
    session = await app.state.pty_manager.create_session(agent_id, mode, cwd, command=command, env=env)
    await app.state.pty_manager.attach(session, websocket)
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
                    int(message.get("cols", session.cols)),
                    int(message.get("rows", session.rows)),
                )
            elif message.get("type") == "detach":
                break
    except WebSocketDisconnect:
        pass
    finally:
        await app.state.pty_manager.detach(session, websocket)
        if session.mode == "hybrid" and not session.connections:
            await app.state.pty_manager.destroy_session(agent_id)
