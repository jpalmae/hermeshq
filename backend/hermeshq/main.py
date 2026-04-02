import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from hermeshq.config import get_settings
from hermeshq.core.events import EventBroker
from hermeshq.core.security import hash_password
from hermeshq.database import AsyncSessionLocal, init_database
from hermeshq.models import Agent, AppSettings, Node, User
from hermeshq.routers import agents, auth, comms, dashboard, logs, nodes, scheduled_tasks, secrets, settings as settings_router, skills, tasks, templates
from hermeshq.schemas.common import HealthResponse
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.comms_router import CommsRouter
from hermeshq.services.hermes_installation import HermesInstallationManager
from hermeshq.services.hermes_runtime import HermesRuntime
from hermeshq.services.pty_manager import PTYManager
from hermeshq.services.scheduler import SchedulerService
from hermeshq.services.secret_vault import SecretVault
from hermeshq.services.workspace_manager import WorkspaceManager

settings = get_settings()


async def bootstrap_defaults() -> None:
    async with AsyncSessionLocal() as session:
        user_result = await session.execute(select(User).where(User.username == settings.admin_username))
        if not user_result.scalar_one_or_none():
            session.add(
                User(
                    username=settings.admin_username,
                    display_name=settings.admin_display_name,
                    password_hash=hash_password(settings.admin_password),
                )
            )
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
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_database()
    await bootstrap_defaults()
    app.state.event_broker = EventBroker()
    app.state.workspace_manager = WorkspaceManager(settings.workspaces_root)
    app.state.secret_vault = SecretVault(settings.fernet_key or settings.jwt_secret)
    app.state.installation_manager = HermesInstallationManager(AsyncSessionLocal, app.state.secret_vault)
    app.state.runtime = HermesRuntime(AsyncSessionLocal, app.state.secret_vault, app.state.installation_manager)
    app.state.supervisor = AgentSupervisor(AsyncSessionLocal, app.state.event_broker, app.state.runtime)
    app.state.comms_router = CommsRouter(AsyncSessionLocal, app.state.event_broker)
    app.state.pty_manager = PTYManager(settings.pty_shell)
    app.state.scheduler = SchedulerService(AsyncSessionLocal, app.state.supervisor.submit_task)
    await app.state.supervisor.bootstrap_runtime()
    await app.state.scheduler.start()
    yield
    await app.state.scheduler.stop()


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
app.include_router(agents.router, prefix=settings.api_prefix)
app.include_router(tasks.router, prefix=settings.api_prefix)
app.include_router(dashboard.router, prefix=settings.api_prefix)
app.include_router(comms.router, prefix=settings.api_prefix)
app.include_router(secrets.router, prefix=settings.api_prefix)
app.include_router(settings_router.router, prefix=settings.api_prefix)
app.include_router(skills.router, prefix=settings.api_prefix)
app.include_router(templates.router, prefix=settings.api_prefix)
app.include_router(logs.router, prefix=settings.api_prefix)
app.include_router(scheduled_tasks.router, prefix=settings.api_prefix)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", timestamp=datetime.now(timezone.utc))


@app.websocket("/ws/stream")
async def stream(websocket: WebSocket) -> None:
    broker: EventBroker = app.state.event_broker
    await broker.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        broker.disconnect(websocket)


@app.websocket("/ws/pty/{agent_id}")
async def pty_stream(websocket: WebSocket, agent_id: str) -> None:
    mode = "hybrid"
    async with AsyncSessionLocal() as session:
        agent = await session.get(Agent, agent_id)
        if not agent:
            await websocket.close(code=4404)
            return
        mode = agent.run_mode
        if mode == "headless":
            await websocket.close(code=4400, reason="Agent is headless")
            return
        cwd = agent.workspace_path
        await app.state.installation_manager.sync_agent_installation(agent)
        env = await app.state.installation_manager.build_process_env(agent)
        command = ["hermes"]
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
