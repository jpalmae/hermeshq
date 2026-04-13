import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from hermeshq.config import get_settings
from hermeshq.core.events import EventBroker
from hermeshq.core.security import get_accessible_agent_ids, get_websocket_user, hash_password, is_admin
from hermeshq.database import AsyncSessionLocal, init_database
from hermeshq.models import Agent, AppSettings, Node, ProviderDefinition, User
from hermeshq.routers import agents, auth, comms, dashboard, hermes_versions, integration_packages, internal_agents, logs, managed_integrations, messaging_channels, nodes, providers, runtime_profiles, scheduled_tasks, secrets, settings as settings_router, skills, tasks, templates, users
from hermeshq.schemas.common import HealthResponse
from hermeshq.services.agent_identity import derive_agent_identity, slugify_agent_value
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.comms_router import CommsRouter
from hermeshq.services.hermes_installation import HermesInstallationManager
from hermeshq.services.hermes_runtime import HermesRuntime
from hermeshq.services.gateway_supervisor import GatewaySupervisor
from hermeshq.services.hermes_version_manager import HermesVersionManager
from hermeshq.services.pty_manager import PTYManager
from hermeshq.services.provider_catalog import BUILTIN_PROVIDERS, seed_provider_defaults
from hermeshq.services.runtime_profiles import normalize_runtime_profile_slug, terminal_allowed_for_profile
from hermeshq.services.scheduler import SchedulerService
from hermeshq.services.secret_vault import SecretVault
from hermeshq.services.workspace_manager import WorkspaceManager

settings = get_settings()


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
            session.add(AppSettings(id="default"))
        else:
            if settings_row.default_hermes_version == "bundled":
                settings_row.default_hermes_version = None
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
    app.state.pty_manager = PTYManager(settings.pty_shell)
    app.state.supervisor.pty_manager = app.state.pty_manager
    app.state.scheduler = SchedulerService(AsyncSessionLocal, app.state.supervisor.submit_task)
    await app.state.supervisor.bootstrap_runtime()
    await app.state.gateway_supervisor.bootstrap_gateways()
    await app.state.scheduler.start()
    yield
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
app.include_router(integration_packages.router, prefix=settings.api_prefix)
app.include_router(managed_integrations.router, prefix=settings.api_prefix)
app.include_router(agents.router, prefix=settings.api_prefix)
app.include_router(tasks.router, prefix=settings.api_prefix)
app.include_router(dashboard.router, prefix=settings.api_prefix)
app.include_router(comms.router, prefix=settings.api_prefix)
app.include_router(internal_agents.router, prefix=settings.api_prefix)
app.include_router(secrets.router, prefix=settings.api_prefix)
app.include_router(settings_router.router, prefix=settings.api_prefix)
app.include_router(skills.router, prefix=settings.api_prefix)
app.include_router(messaging_channels.router, prefix=settings.api_prefix)
app.include_router(templates.router, prefix=settings.api_prefix)
app.include_router(logs.router, prefix=settings.api_prefix)
app.include_router(scheduled_tasks.router, prefix=settings.api_prefix)
app.include_router(users.router, prefix=settings.api_prefix)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", timestamp=datetime.now(timezone.utc))


@app.websocket("/ws/stream")
async def stream(websocket: WebSocket) -> None:
    broker: EventBroker = app.state.event_broker
    async with AsyncSessionLocal() as session:
        user = await get_websocket_user(websocket, session)
        if not user:
            await websocket.close(code=4401)
            return
        accessible_agent_ids = await get_accessible_agent_ids(session, user)
    await broker.connect(websocket, is_admin=is_admin(user), agent_ids=accessible_agent_ids)
    try:
        while True:
            await websocket.receive_text()
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
