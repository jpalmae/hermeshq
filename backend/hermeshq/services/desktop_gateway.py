from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from hermeshq.models.agent import Agent

logger = logging.getLogger(__name__)

PORT_RANGE_START = 9300
PORT_RANGE_END = 9499
STARTUP_TIMEOUT_SECONDS = 90.0
STARTUP_POLL_INTERVAL = 0.5
IDLE_SHUTDOWN_SECONDS = 30 * 60
REAPER_INTERVAL_SECONDS = 60.0


@dataclass
class GatewayHandle:
    agent_id: str
    port: int
    process: asyncio.subprocess.Process
    inner_token: str = ""
    started_at: float = field(default_factory=time.monotonic)
    last_active: float = field(default_factory=time.monotonic)
    ws_connections: int = 0
    log_path: Path | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/api/ws?token={self.inner_token}"

    def touch(self) -> None:
        self.last_active = time.monotonic()


class DesktopGatewayError(RuntimeError):
    pass


class DesktopGatewayService:
    """Manages on-demand ``hermes serve`` processes for Hermes Desktop connections.

    Each agent with desktop access gets a headless gateway bound to loopback,
    spawned on first request and retired after an idle window. The HTTP/WS
    proxy lives in ``routers/desktop_gateway.py``; this service only owns the
    process lifecycle.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        installation_manager,
    ) -> None:
        self.session_factory = session_factory
        self._installation_manager = installation_manager
        self._gateways: dict[str, GatewayHandle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._stopping = False
        self._reaper_task: asyncio.Task | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def shutdown(self) -> None:
        self._stopping = True
        if self._reaper_task:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
            self._reaper_task = None
        for agent_id in list(self._gateways):
            await self.stop_agent(agent_id)

    def _get_lock(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._locks:
            self._locks[agent_id] = asyncio.Lock()
        return self._locks[agent_id]

    # ── public API ───────────────────────────────────────────────────────────

    def get_handle(self, agent_id: str) -> GatewayHandle | None:
        handle = self._gateways.get(agent_id)
        if handle and handle.process.returncode is not None:
            self._cleanup_handle(agent_id, handle)
            return None
        return handle

    async def ensure_agent_gateway(self, agent: Agent) -> GatewayHandle:
        handle = self.get_handle(agent.id)
        if handle is not None:
            handle.touch()
            return handle
        async with self._get_lock(agent.id):
            handle = self.get_handle(agent.id)
            if handle is not None:
                handle.touch()
                return handle
            return await self._spawn_gateway(agent)

    async def stop_agent(self, agent_id: str) -> None:
        handle = self._gateways.pop(agent_id, None)
        if handle is None:
            return
        await self._terminate_handle(handle)

    def track_ws_connection(self, agent_id: str, delta: int) -> None:
        handle = self._gateways.get(agent_id)
        if handle:
            handle.ws_connections = max(0, handle.ws_connections + delta)
            handle.touch()

    def status(self) -> list[dict]:
        return [
            {
                "agent_id": h.agent_id,
                "port": h.port,
                "pid": h.process.pid,
                "uptime_seconds": int(time.monotonic() - h.started_at),
                "idle_seconds": int(time.monotonic() - h.last_active),
                "ws_connections": h.ws_connections,
            }
            for h in self._gateways.values()
        ]

    # ── spawn / teardown ─────────────────────────────────────────────────────

    async def _spawn_gateway(self, agent: Agent) -> GatewayHandle:
        runtime_selection = await self._installation_manager.resolve_hermes_runtime(agent)
        hermes_bin = runtime_selection.hermes_bin
        if not Path(hermes_bin).exists():
            raise DesktopGatewayError(f"Hermes runtime not installed for agent {agent.id}")

        await self._installation_manager.sync_agent_installation(agent)
        env = await self._installation_manager.build_process_env(agent, include_channels=False)
        inner_token = secrets.token_urlsafe(32)
        env["HERMES_DASHBOARD_SESSION_TOKEN"] = inner_token
        hermes_home = self._installation_manager.build_hermes_home(agent.workspace_path)
        logs_dir = hermes_home / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "desktop-serve.log"

        port = self._pick_free_port()
        if port is None:
            raise DesktopGatewayError("No free port available for desktop gateway")

        log_file = log_path.open("ab")
        try:
            process = await asyncio.create_subprocess_exec(
                hermes_bin,
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                cwd=str(hermes_home),
                env=env,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
            )
        finally:
            log_file.close()

        handle = GatewayHandle(
            agent_id=agent.id, port=port, process=process, log_path=log_path, inner_token=inner_token
        )
        self._gateways[agent.id] = handle
        logger.info("Desktop gateway started for agent %s on port %d (pid %s)", agent.id, port, process.pid)

        try:
            await self._wait_ready(handle)
        except Exception:
            await self.stop_agent(agent.id)
            raise
        return handle

    def _pick_free_port(self) -> int | None:
        for candidate in range(PORT_RANGE_START, PORT_RANGE_END):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", candidate))
                except OSError:
                    continue
                return candidate
        return None

    async def _wait_ready(self, handle: GatewayHandle) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.monotonic() < deadline:
                if handle.process.returncode is not None:
                    tail = self._log_tail(handle)
                    raise DesktopGatewayError(
                        f"Desktop gateway for agent {handle.agent_id} exited during startup "
                        f"(code {handle.process.returncode}). Log tail: {tail}"
                    )
                try:
                    response = await client.get(f"{handle.base_url}/api/status")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(STARTUP_POLL_INTERVAL)
        raise DesktopGatewayError(
            f"Desktop gateway for agent {handle.agent_id} did not become ready within "
            f"{STARTUP_TIMEOUT_SECONDS:.0f}s. Log tail: {self._log_tail(handle)}"
        )

    @staticmethod
    def _log_tail(handle: GatewayHandle, max_chars: int = 1200) -> str:
        if not handle.log_path or not handle.log_path.exists():
            return "<no log>"
        try:
            content = handle.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "<unreadable log>"
        return content[-max_chars:]

    async def _terminate_handle(self, handle: GatewayHandle) -> None:
        if handle.process.returncode is None:
            handle.process.terminate()
            try:
                await asyncio.wait_for(handle.process.wait(), timeout=10)
            except TimeoutError:
                handle.process.kill()
                await handle.process.wait()
        logger.info("Desktop gateway stopped for agent %s (port %d)", handle.agent_id, handle.port)

    def _cleanup_handle(self, agent_id: str, handle: GatewayHandle) -> None:
        if self._gateways.get(agent_id) is handle:
            del self._gateways[agent_id]
        logger.info(
            "Desktop gateway for agent %s exited unexpectedly (code %s)",
            agent_id,
            handle.process.returncode,
        )

    # ── idle reaper ──────────────────────────────────────────────────────────

    async def _reaper_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(REAPER_INTERVAL_SECONDS)
                await self._reaper_single_pass()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Desktop gateway reaper tick failed")

    async def _reaper_single_pass(self) -> None:
        now = time.monotonic()
        for agent_id, handle in list(self._gateways.items()):
            idle = now - handle.last_active
            if handle.ws_connections > 0:
                handle.touch()
                continue
            if idle >= IDLE_SHUTDOWN_SECONDS:
                logger.info("Desktop gateway for agent %s idle %.0fs — stopping", agent_id, idle)
                await self.stop_agent(agent_id)


def desktop_gateway_service_factory(session_factory, installation_manager) -> DesktopGatewayService:
    return DesktopGatewayService(session_factory, installation_manager)
