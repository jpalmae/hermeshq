"""Gateway supervisor — orchestrates messaging channel gateway processes.

Public API (used by routers):
    - bootstrap_gateways()
    - shutdown()
    - start_channel(agent_id, platform)
    - stop_channel(agent_id, platform)
    - restart_channel(agent_id, platform)
    - get_runtime_status(agent_id, platform)
    - tail_log(agent_id, platform, lines)
    - set_enterprise_gateways(manager)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.events import EventBroker
from hermeshq.models.agent import Agent
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.services.gateway_log_manager import GatewayLogManager
from hermeshq.services.gateway_process_manager import GatewayProcessManager
from hermeshq.services.gateway_types import GatewayProcessHandle
from hermeshq.services.hermes_installation import HermesInstallationManager

logger = logging.getLogger(__name__)
BOOTSTRAP_CONCURRENCY = 3
BOOTSTRAP_CHANNEL_TIMEOUT_SECONDS = int(os.getenv("HQ_BOOTSTRAP_TIMEOUT", "30"))
BOOTSTRAP_RETRY_ATTEMPTS = int(os.getenv("HQ_BOOTSTRAP_RETRIES", "2"))
BOOTSTRAP_RETRY_DELAYS_SECONDS = (2, 5)


class GatewaySupervisor:
    """Facade that delegates to GatewayProcessManager and GatewayLogManager."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_broker: EventBroker,
        installation_manager: HermesInstallationManager,
    ) -> None:
        self.session_factory = session_factory
        self.event_broker = event_broker
        self.installation_manager = installation_manager
        self.processes: dict[str, GatewayProcessHandle] = {}
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._enterprise_gateways: object | None = None

        # Sub-managers
        self._process_mgr = GatewayProcessManager(
            session_factory=session_factory,
            event_broker=event_broker,
            installation_manager=installation_manager,
            processes=self.processes,
        )
        self._log_mgr = GatewayLogManager(
            session_factory=session_factory,
            event_broker=event_broker,
            installation_manager=installation_manager,
            processes=self.processes,
        )

    def set_enterprise_gateways(self, manager: object) -> None:
        self._enterprise_gateways = manager
        self._process_mgr.set_enterprise_gateways(manager)

    def _get_agent_lock(self, agent_id: str) -> asyncio.Lock:
        lock = self._agent_locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[agent_id] = lock
        return lock

    # ── Bootstrap ───────────────────────────────────────────────────────────

    async def bootstrap_gateways(self) -> None:
        """Bootstrap all gateway channels for active agents."""
        try:
            await self._do_bootstrap_gateways()
        except Exception:  # noqa: BLE001  # bootstrap — any fatal error logged
            logger.exception("Fatal error during gateway bootstrap")

    async def _do_bootstrap_gateways(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Agent).where(
                    Agent.is_archived.is_(False),
                    Agent.status != "stopped",
                )
            )
            agents = list(result.scalars().all())

        semaphore = asyncio.Semaphore(BOOTSTRAP_CONCURRENCY)

        async def _bootstrap_agent(agent: Agent) -> None:
            async with semaphore:
                await self._bootstrap_one(agent)

        await asyncio.gather(*[_bootstrap_agent(a) for a in agents], return_exceptions=True)

    async def _persist_channel_metadata(self, channel_id: str, metadata: dict) -> None:
        """Persist updated metadata_json for a channel using a fresh session."""
        async with self.session_factory() as session:
            await session.execute(
                update(MessagingChannel)
                .where(MessagingChannel.id == channel_id)
                .values(metadata_json=metadata)
            )
            await session.commit()

    async def _bootstrap_one(self, agent: Agent) -> None:
        async with self.session_factory() as session:
            channels = await session.execute(
                select(MessagingChannel)
                .where(MessagingChannel.agent_id == agent.id)
                .order_by(MessagingChannel.platform.asc())
            )
            all_channels = list(channels.scalars().all())

        for channel in all_channels:
            if not self._process_mgr._channel_runtime_enabled(channel):
                continue
            platform = channel.platform
            # Enterprise platforms are managed by their own gateway, skip here
            if platform in ("google_chat", "kapso_whatsapp"):
                continue
            lock = self._get_agent_lock(agent.id)
            async with lock:
                last_exc: Exception | None = None
                for attempt in range(1, BOOTSTRAP_RETRY_ATTEMPTS + 1):
                    try:
                        await asyncio.wait_for(
                            self._process_mgr.start_channel_locked(agent.id, platform, self._log_mgr),
                            timeout=BOOTSTRAP_CHANNEL_TIMEOUT_SECONDS,
                        )
                        self._mark_bootstrap_state(channel, status="success", attempted_at=datetime.now(UTC))
                        last_exc = None
                        break
                    except Exception as exc:  # noqa: BLE001  # bootstrap per-channel — retry logic needs broad catch
                        last_exc = exc
                        is_transient = self._is_transient_bootstrap_error(exc)
                        self._mark_bootstrap_state(
                            channel,
                            status="transient" if is_transient else "permanent",
                            attempted_at=datetime.now(UTC),
                            error=str(exc)[:500],
                            attempts=attempt,
                        )
                        if not is_transient or attempt >= BOOTSTRAP_RETRY_ATTEMPTS:
                            break
                        delay = BOOTSTRAP_RETRY_DELAYS_SECONDS[min(attempt - 1, len(BOOTSTRAP_RETRY_DELAYS_SECONDS) - 1)]
                        await asyncio.sleep(delay)
                if last_exc:
                    logger.warning("Bootstrap failed for %s/%s: %s", agent.id, platform, last_exc)
                # Persist the final bootstrap state; channel is detached so use a fresh session.
                await self._persist_channel_metadata(channel.id, channel.metadata_json or {})

    def _mark_bootstrap_state(
        self,
        channel: MessagingChannel,
        *,
        status: str,
        attempted_at: datetime,
        duration_ms: int | None = None,
        error: str | None = None,
        attempts: int | None = None,
    ) -> None:
        metadata = dict(channel.metadata_json or {})
        previous = metadata.get("bootstrap") or {}
        bootstrap = {
            "last_attempt_at": attempted_at.isoformat(),
            "last_status": status,
            "last_error": error,
            "last_duration_ms": duration_ms,
            "last_attempts": attempts,
            "last_source": "startup",
            "last_success_at": previous.get("last_success_at"),
        }
        if status == "success":
            bootstrap["last_success_at"] = attempted_at.isoformat()
        metadata["bootstrap"] = bootstrap
        channel.metadata_json = metadata

    @staticmethod
    def _is_transient_bootstrap_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        if "pid file race" in msg:
            return True
        if "whatsapp bridge process exited unexpectedly" in msg:
            return True
        if "timed out" in msg:
            return True
        if "resource busy" in msg:
            return True
        return "already running" in msg

    # ── Shutdown ────────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        for _agent_id, handle in list(self.processes.items()):
            await self._process_mgr._terminate_handle(handle)
        self.processes.clear()
        self._agent_locks.clear()

    # ── Public channel operations ───────────────────────────────────────────

    async def start_channel(self, agent_id: str, platform: str) -> None:
        lock = self._get_agent_lock(agent_id)
        async with lock:
            await self._process_mgr.start_channel_locked(agent_id, platform, self._log_mgr)

    async def stop_channel(self, agent_id: str, platform: str) -> None:
        lock = self._get_agent_lock(agent_id)
        async with lock:
            await self._process_mgr.stop_channel_locked(agent_id, platform, self._log_mgr)

    async def restart_channel(self, agent_id: str, platform: str) -> None:
        lock = self._get_agent_lock(agent_id)
        async with lock:
            await self._process_mgr.stop_channel_locked(agent_id, platform, self._log_mgr)
            await self._process_mgr.start_channel_locked(agent_id, platform, self._log_mgr)

    # ── Status & logs ───────────────────────────────────────────────────────

    async def get_runtime_status(self, agent_id: str, platform: str) -> dict:
        if platform in ("google_chat", "kapso_whatsapp"):
            return await self._process_mgr.get_enterprise_runtime_status(agent_id, platform)

        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            channel = await self._process_mgr._get_channel(session, agent_id, platform)
            if not channel:
                return {"status": "missing", "pid": None, "log_path": None}
            bootstrap = dict((channel.metadata_json or {}).get("bootstrap") or {})

        handle = self.processes.get(agent_id)
        running = handle is not None and platform in handle.platforms and handle.process.poll() is None

        result: dict = {
            "status": "running" if running else channel.status,
            "pid": handle.process.pid if running else None,
            "log_path": self._log_mgr.gateway_log_path(agent.workspace_path).as_posix() if agent else None,
            "last_bootstrap_at": bootstrap.get("last_attempt_at"),
            "last_bootstrap_success_at": bootstrap.get("last_success_at"),
            "last_bootstrap_status": bootstrap.get("last_status"),
            "last_bootstrap_error": bootstrap.get("last_error"),
            "last_bootstrap_duration_ms": bootstrap.get("last_duration_ms"),
            "last_bootstrap_attempts": bootstrap.get("last_attempts"),
        }

        if platform == "whatsapp" and agent:
            session_dir = self._log_mgr._whatsapp_session_dir(agent.workspace_path)
            bridge_log_path = self._log_mgr._whatsapp_bridge_log_path(agent.workspace_path)
            pairing_status = self._log_mgr.infer_whatsapp_pairing_status(session_dir, bridge_log_path)
            result["pairing_status"] = pairing_status
            result["paired"] = pairing_status == "paired"
            result["session_path"] = session_dir.as_posix()
            result["bridge_log_path"] = bridge_log_path.as_posix() if bridge_log_path else None
            # Do not serve the QR text once the session is already paired — the
            # stored QR in bridge.log is stale and expired at that point.
            result["pairing_qr_text"] = (
                None if pairing_status == "paired"
                else self._log_mgr._extract_whatsapp_qr_text(bridge_log_path)
            )

        await self._maybe_update_connected_at(channel, running)
        return result

    async def _maybe_update_connected_at(self, channel: MessagingChannel, running: bool) -> None:
        if not running:
            return
        metadata = dict(channel.metadata_json or {})
        if metadata.get("connected_at"):
            return
        metadata["connected_at"] = datetime.now(UTC).isoformat()
        channel.metadata_json = metadata
        await self._persist_channel_metadata(channel.id, metadata)

    async def tail_log(self, agent_id: str, platform: str, lines: int = 120) -> str:
        return await self._log_mgr.tail_log(agent_id, platform, lines)
