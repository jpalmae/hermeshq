"""Gateway process lifecycle management — spawn, stop, monitor gateway subprocesses."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.events import EventBroker
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.base import utcnow
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.services.gateway_types import GatewayProcessHandle
from hermeshq.services.hermes_installation import HermesInstallationError, HermesInstallationManager

logger = logging.getLogger(__name__)

GATEWAY_STARTUP_STABILIZATION_SECONDS = 2
GATEWAY_AUTO_RESTART_MAX_ATTEMPTS = 5
GATEWAY_AUTO_RESTART_MIN_UPTIME = 30  # if process ran longer, reset backoff
GATEWAY_RECOVERY_RETRY_DELAY = 300  # 5 minutes
GATEWAY_RECOVERY_MAX_RETRIES = 3  # max recovery cycles before giving up permanently


class GatewayProcessManager:
    """Manages gateway subprocess lifecycle: start, stop, monitor, terminate."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_broker: EventBroker,
        installation_manager: HermesInstallationManager,
        processes: dict[str, GatewayProcessHandle],
        enterprise_gateways: object | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.event_broker = event_broker
        self.installation_manager = installation_manager
        self.processes = processes
        self._enterprise_gateways = enterprise_gateways

    def set_enterprise_gateways(self, manager: object) -> None:
        self._enterprise_gateways = manager

    # ── DB helpers ──────────────────────────────────────────────────────────

    async def _get_channel(self, session: AsyncSession, agent_id: str, platform: str) -> MessagingChannel | None:
        result = await session.execute(
            select(MessagingChannel).where(
                MessagingChannel.agent_id == agent_id,
                MessagingChannel.platform == platform,
            )
        )
        return result.scalar_one_or_none()

    async def _get_channels(self, session: AsyncSession, agent_id: str) -> list[MessagingChannel]:
        result = await session.execute(
            select(MessagingChannel)
            .where(MessagingChannel.agent_id == agent_id)
            .order_by(MessagingChannel.platform.asc())
        )
        return list(result.scalars().all())

    async def _reload_agent(self, agent_id: str) -> Agent:
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                raise ValueError("Agent not found")
            return agent

    # ── Channel helpers ─────────────────────────────────────────────────────

    def _channel_runtime_enabled(self, channel: MessagingChannel) -> bool:
        metadata = channel.metadata_json if isinstance(channel.metadata_json, dict) else {}
        return bool(channel.enabled) and not bool(metadata.get("runtime_disabled"))

    def _set_runtime_disabled(self, channel: MessagingChannel, disabled: bool) -> None:
        metadata = dict(channel.metadata_json or {})
        if disabled:
            metadata["runtime_disabled"] = True
        else:
            metadata.pop("runtime_disabled", None)
        channel.metadata_json = metadata

    # ── Log event helper ────────────────────────────────────────────────────

    async def _log_channel_event(
        self,
        session: AsyncSession,
        agent: Agent,
        channel: MessagingChannel,
        event_type: str,
        message: str,
        *,
        severity: str = "info",
        details: dict | None = None,
    ) -> None:
        session.add(
            ActivityLog(
                agent_id=agent.id,
                node_id=agent.node_id,
                event_type=event_type,
                message=message,
                severity=severity,
                details=details or {},
            )
        )

    # ── Start channel ───────────────────────────────────────────────────────

    async def start_channel_locked(
        self,
        agent_id: str,
        platform: str,
        log_mgr,  # GatewayLogManager — avoid circular import
    ) -> None:
        """Start a gateway channel (must be called with agent lock held)."""
        if platform in ("google_chat", "kapso_whatsapp"):
            await self._start_enterprise_channel(agent_id, platform)
            return

        async with self.session_factory() as session:
            agent_row = await session.get(Agent, agent_id)
            if not agent_row:
                raise ValueError("Agent not found")
            channels = await self._get_channels(session, agent_id)
            channel = next((item for item in channels if item.platform == platform), None)
            if not channel:
                raise ValueError("Messaging channel not found")

            self._set_runtime_disabled(channel, False)
            if not channel.enabled:
                channel.status = "stopped"
                channel.last_error = None
                await session.commit()
                return

            # Check if a gateway process is already running with this platform.
            # During bootstrap, multiple channels for the same agent share one
            # subprocess. If the process is alive and already handles this
            # platform, there is no need to kill and relaunch — just mark running.
            existing_handle = self.processes.get(agent_id)
            if (
                existing_handle
                and existing_handle.process.poll() is None
                and platform in existing_handle.platforms
            ):
                channel.status = "running"
                channel.last_error = None
                channel.updated_at = utcnow()
                await session.commit()
                await self.event_broker.publish(
                    {"type": "messaging.status_changed", "agent_id": agent_id, "status": "running", "message": platform}
                )
                return

            if platform == "telegram" and not channel.secret_ref:
                channel.status = "error"
                channel.last_error = "Telegram bot token secret is required"
                await self._log_channel_event(
                    session, agent_row, channel,
                    f"channel.{platform}.start_failed",
                    f"{agent_row.name} {platform} gateway failed to start",
                    severity="warning",
                    details={"reason": "missing_secret_ref", "error": channel.last_error},
                )
                await session.commit()
                raise ValueError(channel.last_error)

            if platform == "telegram":
                from hermeshq.models.secret import Secret
                secret_exists = await session.execute(select(Secret.id).where(Secret.name == channel.secret_ref))
                if secret_exists.scalar_one_or_none() is None:
                    channel.status = "error"
                    channel.last_error = f"Telegram bot token secret '{channel.secret_ref}' was not found"
                    await self._log_channel_event(
                        session, agent_row, channel,
                        f"channel.{platform}.start_failed",
                        f"{agent_row.name} {platform} gateway failed to start",
                        severity="warning",
                        details={"reason": "secret_not_found", "error": channel.last_error},
                    )
                    await session.commit()
                    raise ValueError(channel.last_error)

            try:
                await self.installation_manager.sync_agent_installation(agent_row)
            except HermesInstallationError as exc:
                channel.status = "error"
                channel.last_error = str(exc)
                await self._log_channel_event(
                    session, agent_row, channel,
                    f"channel.{platform}.start_failed",
                    f"{agent_row.name} {platform} gateway failed to start",
                    severity="warning",
                    details={"reason": "installation_sync_failed", "error": channel.last_error},
                )
                await session.commit()
                raise ValueError(channel.last_error) from exc

            channels = await self._get_channels(session, agent_id)
            active_channels = [item for item in channels if self._channel_runtime_enabled(item)]

        existing = self.processes.pop(agent_id, None)
        if existing:
            await self._terminate_handle(existing)

        if not active_channels:
            async with self.session_factory() as session:
                channels = await self._get_channels(session, agent_id)
                for item in channels:
                    item.status = "stopped"
                    item.last_error = None
                await session.commit()
            return

        agent_row = await self._reload_agent(agent_id)
        try:
            handle = await self._launch_gateway_process(agent_row, active_channels, log_mgr)
        except ValueError as exc:
            async with self.session_factory() as session:
                agent_row = await session.get(Agent, agent_id)
                channels = await self._get_channels(session, agent_id)
                failed_channel = next((item for item in channels if item.platform == platform), None)
                if failed_channel:
                    failed_channel.status = "error"
                    failed_channel.last_error = str(exc)
                    if agent_row:
                        await self._log_channel_event(
                            session, agent_row, failed_channel,
                            f"channel.{platform}.start_failed",
                            f"{agent_row.name} {platform} gateway failed to start",
                            severity="warning",
                            details={"reason": "gateway_start_failed", "error": str(exc)},
                        )
                await session.commit()
            raise
        self.processes[agent_id] = handle

        async with self.session_factory() as session:
            agent_row = await session.get(Agent, agent_id)
            channels = await self._get_channels(session, agent_id)
            active_platforms = set(handle.platforms)
            for item in channels:
                if item.platform in active_platforms:
                    item.status = "running"
                    item.last_error = None
                    item.updated_at = utcnow()
                    session.add(
                        ActivityLog(
                            agent_id=agent_row.id,
                            node_id=agent_row.node_id,
                            event_type=f"channel.{item.platform}.started",
                            message=f"{agent_row.name} {item.platform} gateway started",
                            details={
                                "platform": item.platform,
                                "pid": handle.process.pid,
                                "active_platforms": sorted(active_platforms),
                            },
                        )
                    )
                elif not self._channel_runtime_enabled(item):
                    item.status = "stopped"
                    item.last_error = None
            await session.commit()

        for item in active_channels:
            await self.event_broker.publish(
                {"type": "messaging.status_changed", "agent_id": agent_id, "status": "running", "message": item.platform}
            )

    # ── Stop channel ────────────────────────────────────────────────────────

    async def stop_channel_locked(
        self,
        agent_id: str,
        platform: str,
        log_mgr=None,  # GatewayLogManager — avoid circular import
    ) -> None:
        """Stop a gateway channel (must be called with agent lock held)."""
        if platform in ("google_chat", "kapso_whatsapp"):
            await self._stop_enterprise_channel(agent_id, platform)
            return

        async with self.session_factory() as session:
            agent_row = await session.get(Agent, agent_id)
            if not agent_row:
                return
            channels = await self._get_channels(session, agent_id)
            channel = next((item for item in channels if item.platform == platform), None)
            if not channel:
                return

            self._set_runtime_disabled(channel, True)
            await session.commit()
            try:
                await self.installation_manager.sync_agent_installation(agent_row)
            except HermesInstallationError:
                logger.exception("Failed to resync agent installation while stopping %s for %s", platform, agent_id)
            remaining_channels = [item for item in channels if self._channel_runtime_enabled(item)]

        existing = self.processes.pop(agent_id, None)
        if existing:
            await self._terminate_handle(existing)

        restarted_handle: GatewayProcessHandle | None = None
        if remaining_channels:
            agent_row = await self._reload_agent(agent_id)
            try:
                restarted_handle = await self._launch_gateway_process(agent_row, remaining_channels, log_mgr)
                self.processes[agent_id] = restarted_handle
            except Exception:
                logger.exception(
                    "Failed to restart gateway for agent %s after stopping %s — remaining channels will be marked stopped",
                    agent_id,
                    platform,
                )

        async with self.session_factory() as session:
            agent_row = await session.get(Agent, agent_id)
            channels = await self._get_channels(session, agent_id)
            active_platforms = set(restarted_handle.platforms) if restarted_handle else set()
            for item in channels:
                if item.platform == platform:
                    item.status = "stopped"
                    item.last_error = None
                elif item.platform in active_platforms:
                    item.status = "running"
                    item.last_error = None
                    item.updated_at = utcnow()
                    session.add(
                        ActivityLog(
                            agent_id=agent_row.id,
                            node_id=agent_row.node_id,
                            event_type=f"channel.{item.platform}.started",
                            message=f"{agent_row.name} {item.platform} gateway started",
                            details={
                                "platform": item.platform,
                                "pid": restarted_handle.process.pid,
                                "active_platforms": sorted(active_platforms),
                            },
                        )
                    )
                else:
                    item.status = "stopped"
                    item.last_error = None

            session.add(
                ActivityLog(
                    agent_id=agent_row.id,
                    node_id=agent_row.node_id,
                    event_type=f"channel.{platform}.stopped",
                    message=f"{agent_row.name} {platform} gateway stopped",
                    details={"platform": platform},
                )
            )
            await session.commit()

        await self.event_broker.publish(
            {"type": "messaging.status_changed", "agent_id": agent_id, "status": "stopped", "message": platform}
        )
        if restarted_handle:
            for remaining_platform in restarted_handle.platforms:
                await self.event_broker.publish(
                    {
                        "type": "messaging.status_changed",
                        "agent_id": agent_id,
                        "status": "running",
                        "message": remaining_platform,
                    }
                )

    # ── Enterprise gateways ─────────────────────────────────────────────────

    async def get_enterprise_runtime_status(self, agent_id: str, platform: str) -> dict:
        if self._enterprise_gateways is None:
            return {"status": "missing", "pid": None, "log_path": None}
        status_info = self._enterprise_gateways.get_status(agent_id, platform)
        running = status_info.get("running", False)

        async with self.session_factory() as session:
            channel = await self._get_channel(session, agent_id, platform)
            channel_status = channel.status if channel else "stopped"
            bootstrap = dict((channel.metadata_json or {}).get("bootstrap") or {}) if channel else {}

        return {
            "status": "running" if running else channel_status,
            "pid": None,
            "log_path": None,
            "last_bootstrap_at": bootstrap.get("last_attempt_at"),
            "last_bootstrap_success_at": bootstrap.get("last_success_at"),
            "last_bootstrap_status": bootstrap.get("last_status"),
            "last_bootstrap_error": bootstrap.get("last_error"),
            "last_bootstrap_duration_ms": bootstrap.get("last_duration_ms"),
            "last_bootstrap_attempts": bootstrap.get("last_attempts"),
            "paired": None,
            "pairing_status": None,
            "session_path": None,
            "bridge_log_path": None,
            "pairing_qr_text": None,
        }

    async def _start_enterprise_channel(self, agent_id: str, platform: str) -> None:
        if self._enterprise_gateways is None:
            raise ValueError(f"Enterprise gateway manager not available for {platform}")
        try:
            await self._enterprise_gateways.start_gateway(agent_id, platform)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ValueError(str(exc)) from exc

        async with self.session_factory() as session:
            channel = await self._get_channel(session, agent_id, platform)
            if channel:
                channel.status = "running"
                channel.last_error = None
                channel.updated_at = utcnow()
                await session.commit()

        await self.event_broker.publish(
            {"type": "messaging.status_changed", "agent_id": agent_id, "status": "running", "message": platform}
        )

    async def _stop_enterprise_channel(self, agent_id: str, platform: str) -> None:
        if self._enterprise_gateways is None:
            return
        await self._enterprise_gateways.stop_gateway(agent_id, platform)

        async with self.session_factory() as session:
            channel = await self._get_channel(session, agent_id, platform)
            if channel:
                channel.status = "stopped"
                channel.last_error = None
                channel.updated_at = utcnow()
                await session.commit()

        await self.event_broker.publish(
            {"type": "messaging.status_changed", "agent_id": agent_id, "status": "stopped", "message": platform}
        )

    # ── Process lifecycle ───────────────────────────────────────────────────

    async def _launch_gateway_process(
        self,
        agent: Agent,
        active_channels: list[MessagingChannel],
        log_mgr,  # GatewayLogManager or None
    ) -> GatewayProcessHandle:
        env = await self.installation_manager.build_gateway_env(agent)
        runtime_selection = await self.installation_manager.resolve_hermes_runtime(agent)
        workspace_path = self.installation_manager.resolve_workspace_path(agent.workspace_path)

        if log_mgr:
            log_mgr.cleanup_stale_gateway_pid(agent.workspace_path)

        log_path = self.gateway_log_path(agent.workspace_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        try:
            process = subprocess.Popen(
                [runtime_selection.hermes_bin, "gateway", "run", "--replace"],
                cwd=str(workspace_path),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
        except Exception:
            log_handle.close()
            raise
        handle = GatewayProcessHandle(
            agent_id=agent.id,
            process=process,
            log_path=log_path.as_posix(),
            log_handle=log_handle,
            platforms={item.platform for item in active_channels},
        )

        if log_mgr:
            sessions_dir = log_mgr.sessions_dir(agent.workspace_path)
            for item in active_channels:
                known_activity_keys, session_file_state = await asyncio.to_thread(
                    log_mgr.snapshot_session_activity,
                    sessions_dir,
                    item.platform,
                )
                handle.known_activity_keys[item.platform] = known_activity_keys
                handle.session_file_state[item.platform] = session_file_state
                handle.activity_tasks[item.platform] = asyncio.create_task(
                    log_mgr.activity_sync_loop(
                        agent.id,
                        agent.node_id,
                        str(workspace_path),
                        item.platform,
                        known_activity_keys,
                        session_file_state,
                    )
                )

        handle.monitor_task = asyncio.create_task(
            self._monitor_process(
                agent.id,
                process,
                log_path.as_posix(),
                log_handle,
                set(handle.platforms),
                log_mgr,
            )
        )
        try:
            await self._wait_for_gateway_startup(handle)
        except Exception:  # noqa: BLE001
            await self._terminate_handle(handle)
            raise
        return handle

    async def _wait_for_gateway_startup(self, handle: GatewayProcessHandle) -> None:
        await asyncio.sleep(GATEWAY_STARTUP_STABILIZATION_SECONDS)
        return_code = handle.process.poll()
        if return_code is None:
            return

        log_tail = self._read_log_tail(Path(handle.log_path), lines=80).lower()
        if "pid file race lost to another gateway instance" in log_tail:
            raise ValueError("PID file race lost to another gateway instance")
        if "whatsapp bridge process exited unexpectedly" in log_tail:
            raise ValueError("WhatsApp bridge process exited unexpectedly during startup")

        last_line = ""
        for line in reversed(log_tail.splitlines()):
            stripped = line.strip()
            if stripped:
                last_line = stripped
                break
        if last_line:
            raise ValueError(f"Gateway exited during startup with code {return_code}: {last_line}")
        raise ValueError(f"Gateway exited during startup with code {return_code}")

    async def _terminate_handle(self, handle: GatewayProcessHandle) -> None:
        # Cancel and await all background tasks to prevent fire-and-forget
        # that could lead to double-close of log_handle or use-after-free.
        tasks_to_await: list[asyncio.Task] = []
        if handle.monitor_task:
            handle.monitor_task.cancel()
            tasks_to_await.append(handle.monitor_task)
        for task in handle.activity_tasks.values():
            task.cancel()
            tasks_to_await.append(task)
        # Wait for all tasks to actually finish (suppressing CancelledError)
        if tasks_to_await:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
        if handle.process.poll() is None:
            handle.process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(handle.process.wait), timeout=5)
            except TimeoutError:
                handle.process.kill()
                await asyncio.to_thread(handle.process.wait)
        with contextlib.suppress(Exception):
            handle.log_handle.close()

    async def _monitor_process(
        self,
        agent_id: str,
        process: subprocess.Popen,
        log_path: str,
        log_handle,
        platforms: set[str],
        log_mgr=None,
    ) -> None:
        start_time = time.monotonic()

        try:
            return_code = await asyncio.to_thread(process.wait)
        except asyncio.CancelledError:
            return
        finally:
            with contextlib.suppress(Exception):
                log_handle.flush()
                log_handle.close()

        uptime = time.monotonic() - start_time

        handle = self.processes.get(agent_id)
        if handle and handle.process is process:
            self.processes.pop(agent_id, None)
            for task in handle.activity_tasks.values():
                task.cancel()

        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                return
            channels = await self._get_channels(session, agent_id)
            for channel in channels:
                if channel.platform not in platforms:
                    continue
                channel.status = "stopped" if return_code == 0 else "error"
                channel.last_error = None if return_code == 0 else f"{channel.platform} gateway exited with code {return_code}"
                session.add(
                    ActivityLog(
                        agent_id=agent.id,
                        node_id=agent.node_id,
                        event_type=f"channel.{channel.platform}.exited",
                        message=f"{agent.name} {channel.platform} gateway exited",
                        details={"platform": channel.platform, "return_code": return_code, "log_path": log_path, "uptime": round(uptime, 1)},
                    )
                )
            await session.commit()

        for platform in platforms:
            await self.event_broker.publish(
                {
                    "type": "messaging.status_changed",
                    "agent_id": agent_id,
                    "status": "stopped" if return_code == 0 else "error",
                    "message": platform,
                }
            )

        # ── Auto-restart logic ──────────────────────────────────────────────
        # Check if channels are still supposed to be running (not explicitly stopped)
        should_restart = False
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if agent and not agent.is_archived:
                channels = await self._get_channels(session, agent_id)
                should_restart = any(
                    self._channel_runtime_enabled(ch) and ch.platform in platforms
                    for ch in channels
                )

        if not should_restart:
            return

        if agent_id in self.processes:
            logger.info("Gateway for agent %s already relaunched — skipping auto-restart", agent_id)
            return

        logger.warning(
            "Gateway for agent %s exited unexpectedly (rc=%d, uptime=%.0fs) — scheduling auto-restart",
            agent_id, return_code, uptime,
        )

        await self._auto_restart_gateway(agent_id, platforms, uptime, log_mgr)

    async def _auto_restart_gateway(
        self,
        agent_id: str,
        platforms: set[str],
        uptime: float,
        log_mgr,
        _recovery_attempt: int = 0,
    ) -> None:
        """Auto-restart gateway after unexpected exit, with exponential backoff."""

        # If the process ran stably for a while, reset any previous retry state
        if uptime > GATEWAY_AUTO_RESTART_MIN_UPTIME:
            logger.info("Gateway was stable for %.0fs — starting fresh restart cycle", uptime)

        for attempt in range(GATEWAY_AUTO_RESTART_MAX_ATTEMPTS):
            backoff = min(5 * (2 ** attempt), 60)

            logger.warning(
                "Gateway auto-restart for agent %s: attempt %d/%d in %ds",
                agent_id, attempt + 1, GATEWAY_AUTO_RESTART_MAX_ATTEMPTS, backoff,
            )

            await asyncio.sleep(backoff)

            # Re-check if channels are still supposed to be running
            async with self.session_factory() as session:
                agent_row = await session.get(Agent, agent_id)
                if not agent_row or agent_row.is_archived:
                    logger.info("Agent %s gone or archived — cancelling auto-restart", agent_id)
                    return

                channels = await self._get_channels(session, agent_id)
                active = [
                    ch for ch in channels
                    if self._channel_runtime_enabled(ch) and ch.platform in platforms
                ]

                if not active:
                    logger.info("Channels for agent %s were stopped during backoff — not restarting", agent_id)
                    return

            # Check if something else already relaunched the gateway
            if agent_id in self.processes:
                logger.info("Gateway for agent %s already running — skipping restart", agent_id)
                return

            # Attempt restart
            try:
                agent_row = await self._reload_agent(agent_id)
                await self.installation_manager.sync_agent_installation(agent_row)

                async with self.session_factory() as session:
                    channels = await self._get_channels(session, agent_id)
                    active_channels = [ch for ch in channels if self._channel_runtime_enabled(ch)]

                agent_row = await self._reload_agent(agent_id)
                handle = await self._launch_gateway_process(agent_row, active_channels, log_mgr)
                self.processes[agent_id] = handle

                # Update status to running
                async with self.session_factory() as session:
                    agent_row = await session.get(Agent, agent_id)
                    channels = await self._get_channels(session, agent_id)
                    for ch in channels:
                        if ch.platform in handle.platforms:
                            ch.status = "running"
                            ch.last_error = None
                            ch.updated_at = utcnow()
                            session.add(ActivityLog(
                                agent_id=agent_row.id,
                                node_id=agent_row.node_id,
                                event_type=f"channel.{ch.platform}.auto_restarted",
                                message=f"{agent_row.name} {ch.platform} gateway auto-restarted after unexpected exit",
                                details={"platform": ch.platform, "pid": handle.process.pid},
                            ))
                    await session.commit()

                for platform in handle.platforms:
                    await self.event_broker.publish(
                        {"type": "messaging.status_changed", "agent_id": agent_id, "status": "running", "message": platform}
                    )

                logger.info("Gateway for agent %s auto-restarted successfully on attempt %d", agent_id, attempt + 1)
                return

            except Exception:
                logger.exception(
                    "Gateway auto-restart attempt %d/%d failed for agent %s",
                    attempt + 1, GATEWAY_AUTO_RESTART_MAX_ATTEMPTS, agent_id,
                )

        # All attempts exhausted — mark error and schedule a recovery retry
        logger.error("Gateway auto-restart exhausted for agent %s after %d attempts — scheduling recovery in %ds",
                     agent_id, GATEWAY_AUTO_RESTART_MAX_ATTEMPTS, GATEWAY_RECOVERY_RETRY_DELAY)

        async with self.session_factory() as session:
            agent_row = await session.get(Agent, agent_id)
            if not agent_row:
                return
            channels = await self._get_channels(session, agent_id)
            for ch in channels:
                if ch.platform in platforms:
                    ch.status = "error"
                    ch.last_error = "Gateway crashed repeatedly — automatic recovery scheduled"
                    session.add(ActivityLog(
                        agent_id=agent_row.id,
                        node_id=agent_row.node_id,
                        event_type=f"channel.{ch.platform}.auto_restart_failed",
                        message=f"{agent_row.name} {ch.platform} gateway auto-restart failed after {GATEWAY_AUTO_RESTART_MAX_ATTEMPTS} attempts",
                        severity="error",
                    ))
            await session.commit()

            for platform in platforms:
                await self.event_broker.publish(
                    {"type": "messaging.status_changed", "agent_id": agent_id, "status": "error", "message": platform}
                )

        await asyncio.sleep(GATEWAY_RECOVERY_RETRY_DELAY)
        if agent_id in self.processes:
            return
        _recovery_attempt += 1
        if _recovery_attempt > GATEWAY_RECOVERY_MAX_RETRIES:
            logger.error("Gateway recovery exhausted for agent %s after %d recovery cycles — giving up permanently",
                         agent_id, _recovery_attempt - 1)
            return
        logger.info("Gateway recovery retry %d/%d for agent %s after cooldown",
                    _recovery_attempt, GATEWAY_RECOVERY_MAX_RETRIES, agent_id)
        await self._auto_restart_gateway(agent_id, platforms, 0, log_mgr, _recovery_attempt=_recovery_attempt)

    # ── Path helpers ────────────────────────────────────────────────────────

    def gateway_log_path(self, workspace_path: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "logs" / "gateway.log"

    @staticmethod
    def _read_log_tail(path: Path, lines: int = 120) -> str:
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(content[-lines:])
        except OSError:
            return ""
