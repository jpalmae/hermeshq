import asyncio
import contextlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.events import EventBroker
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.base import utcnow
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.secret import Secret
from hermeshq.services.hermes_installation import HermesInstallationError, HermesInstallationManager

logger = logging.getLogger(__name__)
BOOTSTRAP_CONCURRENCY = 3
BOOTSTRAP_CHANNEL_TIMEOUT_SECONDS = 120
BOOTSTRAP_RETRY_ATTEMPTS = 3
BOOTSTRAP_RETRY_DELAYS_SECONDS = (2, 5)
GATEWAY_STARTUP_STABILIZATION_SECONDS = 2


@dataclass
class GatewayProcessHandle:
    agent_id: str
    process: subprocess.Popen
    log_path: str
    log_handle: object
    platforms: set[str] = field(default_factory=set)
    monitor_task: asyncio.Task | None = None
    activity_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    known_activity_keys: dict[str, set[str]] = field(default_factory=dict)
    session_file_state: dict[str, dict[str, tuple[int, int]]] = field(default_factory=dict)


class GatewaySupervisor:
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

    def set_enterprise_gateways(self, manager: object) -> None:
        """Inject the EnterpriseGatewayManager after construction."""
        self._enterprise_gateways = manager

    def _get_agent_lock(self, agent_id: str) -> asyncio.Lock:
        lock = self._agent_locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_locks[agent_id] = lock
        return lock

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
        metadata["bootstrap"] = {
            "last_attempt_at": attempted_at.isoformat(),
            "last_status": status,
            "last_error": error,
            "last_duration_ms": duration_ms,
            "last_attempts": attempts,
            "last_source": "startup",
            "last_success_at": (
                attempted_at.isoformat()
                if status == "success"
                else (str((metadata.get("bootstrap") or {}).get("last_success_at") or "").strip() or None)
            ),
        }
        channel.metadata_json = metadata

    def _is_transient_bootstrap_error(self, error_message: str) -> bool:
        message = (error_message or "").strip().lower()
        if not message:
            return False
        transient_markers = (
            "pid file race",
            "race lost",
            "timeout",
            "timed out",
            "temporarily unavailable",
            "resource busy",
            "already running",
        )
        return any(marker in message for marker in transient_markers)

    async def bootstrap_gateways(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(MessagingChannel, Agent)
                .join(Agent, Agent.id == MessagingChannel.agent_id)
                .where(MessagingChannel.enabled.is_(True))
            )
            rows = result.all()

        bootstrap_targets: dict[str, tuple[Agent, str]] = {}
        for channel, agent in rows:
            # Enterprise platforms are bootstrapped by EnterpriseGatewayManager
            if channel.platform in ("microsoft_teams", "google_chat"):
                continue
            if not self._channel_runtime_enabled(channel):
                continue
            bootstrap_targets.setdefault(agent.id, (agent, channel.platform))

        semaphore = asyncio.Semaphore(BOOTSTRAP_CONCURRENCY)

        async def _bootstrap_one(agent: Agent, platform: str) -> None:
            async with semaphore:
                attempt = 0
                while attempt < BOOTSTRAP_RETRY_ATTEMPTS:
                    attempt += 1
                    started_at = datetime.now(timezone.utc)
                    try:
                        await asyncio.wait_for(
                            self.start_channel(agent, platform),
                            timeout=BOOTSTRAP_CHANNEL_TIMEOUT_SECONDS,
                        )
                        async with self.session_factory() as session:
                            session_channel = await self._get_channel(session, agent.id, platform)
                            if session_channel:
                                duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
                                self._mark_bootstrap_state(
                                    session_channel,
                                    status="success",
                                    attempted_at=started_at,
                                    duration_ms=duration_ms,
                                    attempts=attempt,
                                )
                                await session.commit()
                        return
                    except asyncio.TimeoutError:
                        error_text = (
                            f"{platform} gateway bootstrap timed out after "
                            f"{BOOTSTRAP_CHANNEL_TIMEOUT_SECONDS} seconds"
                        )
                        transient = True
                        log_event = f"channel.{platform}.bootstrap_timeout"
                        log_message = f"{agent.name} {platform} gateway bootstrap timed out"
                    except ValueError as exc:
                        error_text = str(exc)
                        transient = self._is_transient_bootstrap_error(error_text)
                        log_event = f"channel.{platform}.bootstrap_failed"
                        log_message = f"{agent.name} {platform} gateway bootstrap failed"
                    except Exception:
                        logger.exception(
                            "Unexpected gateway bootstrap failure for agent %s (%s)",
                            agent.id,
                            agent.name,
                        )
                        error_text = "unexpected_error"
                        transient = False
                        log_event = f"channel.{platform}.bootstrap_failed"
                        log_message = f"{agent.name} {platform} gateway bootstrap failed"

                    logger.warning(
                        "%s gateway bootstrap failed for agent %s (%s), attempt %s/%s: %s",
                        platform,
                        agent.id,
                        agent.name,
                        attempt,
                        BOOTSTRAP_RETRY_ATTEMPTS,
                        error_text,
                    )
                    async with self.session_factory() as session:
                        session_agent = await session.get(Agent, agent.id)
                        session_channel = await self._get_channel(session, agent.id, platform)
                        if session_agent and session_channel:
                            session_channel.status = "error"
                            session_channel.last_error = error_text
                            duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
                            self._mark_bootstrap_state(
                                session_channel,
                                status="retrying" if transient and attempt < BOOTSTRAP_RETRY_ATTEMPTS else "failed",
                                attempted_at=started_at,
                                duration_ms=duration_ms,
                                error=error_text,
                                attempts=attempt,
                            )
                            await self._log_channel_event(
                                session,
                                session_agent,
                                session_channel,
                                log_event,
                                log_message,
                                severity="warning",
                                details={
                                    "error": error_text,
                                    "attempt": attempt,
                                    "max_attempts": BOOTSTRAP_RETRY_ATTEMPTS,
                                    "will_retry": bool(transient and attempt < BOOTSTRAP_RETRY_ATTEMPTS),
                                },
                            )
                            await session.commit()
                    if not transient or attempt >= BOOTSTRAP_RETRY_ATTEMPTS:
                        return
                    await asyncio.sleep(
                        BOOTSTRAP_RETRY_DELAYS_SECONDS[
                            min(attempt - 1, len(BOOTSTRAP_RETRY_DELAYS_SECONDS) - 1)
                        ]
                    )

        await asyncio.gather(*(_bootstrap_one(agent, platform) for agent, platform in bootstrap_targets.values()))

    async def shutdown(self) -> None:
        for agent_id, handle in list(self.processes.items()):
            await self._terminate_handle(handle)
            self.processes.pop(agent_id, None)
            async with self.session_factory() as session:
                agent = await session.get(Agent, agent_id)
                if not agent:
                    continue
                channels = await self._get_channels(session, agent_id)
                for channel in channels:
                    if channel.platform not in handle.platforms:
                        continue
                    channel.status = "stopped"
                    channel.last_error = None
                await session.commit()

    async def get_runtime_status(self, agent_id: str, platform: str) -> dict:
        # Delegate enterprise platforms to the EnterpriseGatewayManager
        if platform in ("microsoft_teams", "google_chat") and self._enterprise_gateways is not None:
            return await self._get_enterprise_runtime_status(agent_id, platform)

        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            channel = await self._get_channel(session, agent_id, platform)
            if not channel:
                return {"status": "missing", "pid": None, "log_path": None}
            bootstrap = dict((channel.metadata_json or {}).get("bootstrap") or {})

        handle = self.processes.get(agent_id)
        running = bool(handle and handle.process.poll() is None and platform in handle.platforms)
        session_path = self._whatsapp_session_dir(agent.workspace_path) if agent and platform == "whatsapp" else None
        bridge_log_path = self._whatsapp_bridge_log_path(agent.workspace_path) if agent and platform == "whatsapp" else None
        paired = bool(session_path and (session_path / "creds.json").exists())
        pairing_status = self._infer_whatsapp_pairing_status(session_path, bridge_log_path) if platform == "whatsapp" else None
        pairing_qr_text = self._extract_whatsapp_qr_text(bridge_log_path) if platform == "whatsapp" else None
        status = "running" if running else channel.status
        if platform == "whatsapp" and pairing_status == "waiting_scan":
            status = "pairing"

        return {
            "status": status,
            "pid": handle.process.pid if running and handle else None,
            "log_path": self._gateway_log_path(agent.workspace_path).as_posix() if agent else None,
            "last_bootstrap_at": bootstrap.get("last_attempt_at"),
            "last_bootstrap_success_at": bootstrap.get("last_success_at"),
            "last_bootstrap_status": bootstrap.get("last_status"),
            "last_bootstrap_error": bootstrap.get("last_error"),
            "last_bootstrap_duration_ms": bootstrap.get("last_duration_ms"),
            "last_bootstrap_attempts": bootstrap.get("last_attempts"),
            "paired": paired if platform == "whatsapp" else None,
            "pairing_status": pairing_status,
            "session_path": session_path.as_posix() if session_path else None,
            "bridge_log_path": bridge_log_path.as_posix() if bridge_log_path else None,
            "pairing_qr_text": pairing_qr_text,
        }

    def _channel_log_details(self, platform: str, channel: MessagingChannel, extra: dict | None = None) -> dict:
        details = {
            "platform": platform,
            "secret_ref": channel.secret_ref,
            "enabled": channel.enabled,
        }
        if extra:
            details.update(extra)
        return details

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
                severity=severity,
                message=message,
                details=self._channel_log_details(channel.platform, channel, details),
            )
        )

    async def start_channel(self, agent: Agent | str, platform: str) -> None:
        agent_id = agent.id if isinstance(agent, Agent) else agent
        async with self._get_agent_lock(agent_id):
            await self._start_channel_locked(agent_id, platform)

    async def _start_channel_locked(self, agent_id: str, platform: str) -> None:
        # Delegate enterprise platforms
        if platform in ("microsoft_teams", "google_chat"):
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

            if platform == "telegram" and not channel.secret_ref:
                channel.status = "error"
                channel.last_error = "Telegram bot token secret is required"
                await self._log_channel_event(
                    session,
                    agent_row,
                    channel,
                    f"channel.{platform}.start_failed",
                    f"{agent_row.name} {platform} gateway failed to start",
                    severity="warning",
                    details={"reason": "missing_secret_ref", "error": channel.last_error},
                )
                await session.commit()
                raise ValueError(channel.last_error)

            if platform == "telegram":
                secret_exists = await session.execute(select(Secret.id).where(Secret.name == channel.secret_ref))
                if secret_exists.scalar_one_or_none() is None:
                    channel.status = "error"
                    channel.last_error = f"Telegram bot token secret '{channel.secret_ref}' was not found"
                    await self._log_channel_event(
                        session,
                        agent_row,
                        channel,
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
                    session,
                    agent_row,
                    channel,
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
            handle = await self._launch_gateway_process(agent_row, active_channels)
        except ValueError as exc:
            async with self.session_factory() as session:
                agent_row = await session.get(Agent, agent_id)
                channels = await self._get_channels(session, agent_id)
                active_platforms = {item.platform for item in active_channels}
                for item in channels:
                    if item.platform in active_platforms:
                        item.status = "error"
                        item.last_error = str(exc)
                failed_channel = next((item for item in channels if item.platform == platform), None)
                if agent_row and failed_channel:
                    await self._log_channel_event(
                        session,
                        agent_row,
                        failed_channel,
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

    async def stop_channel(self, agent_id: str, platform: str) -> None:
        async with self._get_agent_lock(agent_id):
            await self._stop_channel_locked(agent_id, platform)

    async def _stop_channel_locked(self, agent_id: str, platform: str) -> None:
        # Delegate enterprise platforms
        if platform in ("microsoft_teams", "google_chat"):
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
            restarted_handle = await self._launch_gateway_process(agent_row, remaining_channels)
            self.processes[agent_id] = restarted_handle

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

    async def restart_channel(self, agent_id: str, platform: str) -> None:
        await self.start_channel(agent_id, platform)

    # ------------------------------------------------------------------
    # Enterprise gateway delegation (Microsoft Teams, Google Chat)
    # ------------------------------------------------------------------

    async def _get_enterprise_runtime_status(self, agent_id: str, platform: str) -> dict:
        """Get runtime status from the EnterpriseGatewayManager."""
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
        """Start an enterprise gateway via the EnterpriseGatewayManager."""
        if self._enterprise_gateways is None:
            raise ValueError(f"Enterprise gateway manager not available for {platform}")
        try:
            await self._enterprise_gateways.start_gateway(agent_id, platform)
        except ValueError:
            raise
        except Exception as exc:
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
        """Stop an enterprise gateway via the EnterpriseGatewayManager."""
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

    async def tail_log(self, agent_id: str, platform: str, lines: int = 120) -> str:
        # Enterprise gateways run as async tasks, not subprocesses — no file logs
        if platform in ("microsoft_teams", "google_chat"):
            return ""

        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            channel = await self._get_channel(session, agent_id, platform)
            if not channel or not agent:
                return ""
            log_path = self._gateway_log_path(agent.workspace_path)

        if not log_path.exists() and platform != "whatsapp":
            return ""
        if platform == "whatsapp":
            sections: list[str] = []
            if log_path.exists():
                gateway_content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if gateway_content:
                    sections.append("[gateway]")
                    sections.extend(gateway_content[-lines:])
            bridge_log_path = self._whatsapp_bridge_log_path(agent.workspace_path)
            if bridge_log_path.exists():
                bridge_content = bridge_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if bridge_content:
                    if sections:
                        sections.append("")
                    sections.append("[bridge]")
                    sections.extend(bridge_content[-lines:])
            return "\n".join(sections)

        if not log_path.exists():
            return ""
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])

    async def _reload_agent(self, agent_id: str) -> Agent:
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                raise ValueError("Agent not found")
            return agent

    async def _launch_gateway_process(
        self,
        agent: Agent,
        active_channels: list[MessagingChannel],
    ) -> GatewayProcessHandle:
        env = await self.installation_manager.build_gateway_env(agent)
        runtime_selection = await self.installation_manager.resolve_hermes_runtime(agent)
        workspace_path = self.installation_manager.resolve_workspace_path(agent.workspace_path)
        self._cleanup_stale_gateway_pid(agent.workspace_path)
        log_path = self._gateway_log_path(agent.workspace_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            [runtime_selection.hermes_bin, "gateway", "run", "--replace"],
            cwd=str(workspace_path),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
        handle = GatewayProcessHandle(
            agent_id=agent.id,
            process=process,
            log_path=log_path.as_posix(),
            log_handle=log_handle,
            platforms={item.platform for item in active_channels},
        )

        sessions_dir = self._sessions_dir(agent.workspace_path)
        for item in active_channels:
            known_activity_keys, session_file_state = await asyncio.to_thread(
                self._snapshot_session_activity,
                sessions_dir,
                item.platform,
            )
            handle.known_activity_keys[item.platform] = known_activity_keys
            handle.session_file_state[item.platform] = session_file_state
            handle.activity_tasks[item.platform] = asyncio.create_task(
                self._activity_sync_loop(
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
            )
        )
        try:
            await self._wait_for_gateway_startup(handle)
        except Exception:
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
        if handle.monitor_task:
            handle.monitor_task.cancel()
        for task in handle.activity_tasks.values():
            task.cancel()
        if handle.process.poll() is None:
            handle.process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(handle.process.wait), timeout=5)
            except asyncio.TimeoutError:
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
    ) -> None:
        try:
            return_code = await asyncio.to_thread(process.wait)
        except asyncio.CancelledError:
            return
        finally:
            with contextlib.suppress(Exception):
                log_handle.flush()
                log_handle.close()

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
                        details={"platform": channel.platform, "return_code": return_code, "log_path": log_path},
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

    async def _activity_sync_loop(
        self,
        agent_id: str,
        node_id: str | None,
        workspace_path: str,
        platform: str,
        known_activity_keys: set[str],
        session_file_state: dict[str, tuple[int, int]],
    ) -> None:
        sessions_dir = self._sessions_dir(workspace_path)
        while True:
            try:
                await asyncio.sleep(5)
                new_entries = await asyncio.to_thread(
                    self._collect_new_session_activity,
                    sessions_dir,
                    platform,
                    known_activity_keys,
                    session_file_state,
                )
                if not new_entries:
                    continue
                async with self.session_factory() as session:
                    existing_keys = {
                        source_key
                        for source_key in await self._recent_activity_source_keys(session, agent_id, platform)
                        if source_key
                    }
                    for entry in new_entries:
                        if entry["key"] in existing_keys:
                            continue
                        session.add(
                            ActivityLog(
                                agent_id=agent_id,
                                node_id=node_id,
                                event_type=f"channel.{platform}.{entry['direction']}",
                                severity="info",
                                message=entry["content"],
                                details={
                                    "platform": platform,
                                    "direction": entry["direction"],
                                    "session_id": entry["session_id"],
                                    "session_file": entry["session_file"],
                                    "session_format": entry["session_format"],
                                    "message_index": entry["message_index"],
                                    "message_timestamp": entry.get("message_timestamp"),
                                    "source_key": entry["key"],
                                },
                            )
                        )
                    await session.commit()
                for entry in new_entries:
                    if entry["key"] in existing_keys:
                        continue
                    await self.event_broker.publish(
                        {
                            "type": "messaging.activity",
                            "agent_id": agent_id,
                            "message": entry["content"],
                            "platform": platform,
                            "direction": entry["direction"],
                        }
                    )
            except asyncio.CancelledError:
                return
            except Exception:
                continue

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

    async def _recent_activity_source_keys(self, session: AsyncSession, agent_id: str, platform: str) -> list[str]:
        result = await session.execute(
            select(ActivityLog.details)
            .where(
                ActivityLog.agent_id == agent_id,
                ActivityLog.event_type.in_((f"channel.{platform}.inbound", f"channel.{platform}.outbound")),
            )
            .order_by(desc(ActivityLog.created_at))
            .limit(1000)
        )
        keys: list[str] = []
        for details in result.scalars():
            if isinstance(details, dict):
                source_key = details.get("source_key")
                if isinstance(source_key, str) and source_key:
                    keys.append(source_key)
        return keys

    def _gateway_log_path(self, workspace_path: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "logs" / "gateway.log"

    def _read_log_tail(self, path: Path, lines: int = 80) -> str:
        if not path.exists():
            return ""
        try:
            return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except Exception:
            return ""

    def _cleanup_stale_gateway_pid(self, workspace_path: str) -> None:
        pid_path = self.installation_manager.build_hermes_home(workspace_path) / "gateway.pid"
        if not pid_path.exists():
            return
        try:
            payload = json.loads(pid_path.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            recorded_start = payload.get("start_time")
        except Exception:
            pid_path.unlink(missing_ok=True)
            return

        proc_dir = Path(f"/proc/{pid}")
        if not proc_dir.exists():
            pid_path.unlink(missing_ok=True)
            return

        if recorded_start is None:
            return

        stat_path = proc_dir / "stat"
        try:
            current_start = int(stat_path.read_text(encoding="utf-8").split()[21])
        except Exception:
            return
        if current_start != recorded_start:
            pid_path.unlink(missing_ok=True)
            return

        cmdline_path = proc_dir / "cmdline"
        try:
            cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip().lower()
        except Exception:
            return
        if "hermes" not in cmdline or "gateway" not in cmdline:
            pid_path.unlink(missing_ok=True)

    def _sessions_dir(self, workspace_path: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "sessions"

    def _whatsapp_session_dir(self, workspace_path: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "whatsapp" / "session"

    def _whatsapp_bridge_log_path(self, workspace_path: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "whatsapp" / "bridge.log"

    def _infer_whatsapp_pairing_status(
        self,
        session_path: Path | None,
        bridge_log_path: Path | None,
    ) -> str | None:
        if not session_path:
            return None
        if (session_path / "creds.json").exists():
            return "paired"
        if bridge_log_path and bridge_log_path.exists():
            try:
                tail = "\n".join(bridge_log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]).lower()
            except Exception:
                tail = ""
            if "waiting for scan" in tail or "scan this qr code" in tail:
                return "waiting_scan"
        return "unpaired"

    def _extract_whatsapp_qr_text(self, bridge_log_path: Path | None) -> str | None:
        if not bridge_log_path or not bridge_log_path.exists():
            return None
        try:
            lines = bridge_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return None

        candidates: list[list[str]] = []
        current: list[str] = []
        for line in lines:
            stripped = line.rstrip()
            if stripped and all(ch in "█▀▄ ▄" for ch in stripped):
                current.append(stripped)
                continue
            if current:
                if len(current) >= 12:
                    candidates.append(current[:])
                current = []
        if current and len(current) >= 12:
            candidates.append(current[:])

        if not candidates:
            return None
        return "\n".join(candidates[-1])

    def _snapshot_session_activity(
        self,
        sessions_dir: Path,
        platform: str,
    ) -> tuple[set[str], dict[str, tuple[int, int]]]:
        known_activity_keys: set[str] = set()
        session_file_state: dict[str, tuple[int, int]] = {}
        if not sessions_dir.exists():
            return known_activity_keys, session_file_state
        for path in sorted(sessions_dir.glob("*.jsonl")):
            if not path.is_file():
                continue
            stat = path.stat()
            session_file_state[path.as_posix()] = (stat.st_mtime_ns, stat.st_size)
            for entry in self._read_session_entries(path, platform):
                known_activity_keys.add(entry["key"])
        return known_activity_keys, session_file_state

    def _collect_new_session_activity(
        self,
        sessions_dir: Path,
        platform: str,
        known_activity_keys: set[str],
        session_file_state: dict[str, tuple[int, int]],
    ) -> list[dict]:
        if not sessions_dir.exists():
            return []

        new_entries: list[dict] = []
        current_files: set[str] = set()
        for path in sorted(sessions_dir.glob("*.jsonl")):
            if not path.is_file():
                continue
            current_files.add(path.as_posix())
            stat = path.stat()
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            if session_file_state.get(path.as_posix()) == fingerprint:
                continue
            session_file_state[path.as_posix()] = fingerprint
            for entry in self._read_session_entries(path, platform):
                if entry["key"] in known_activity_keys:
                    continue
                known_activity_keys.add(entry["key"])
                new_entries.append(entry)

        for tracked in list(session_file_state):
            if tracked not in current_files:
                session_file_state.pop(tracked, None)
        return new_entries

    def _read_session_entries(self, path: Path, platform: str) -> list[dict]:
        try:
            if path.suffix != ".jsonl":
                return []
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not lines:
                return []
            payloads = []
            for line in lines:
                try:
                    payloads.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            if not payloads or payloads[0].get("platform") != platform:
                return []
            messages = [item for item in payloads if item.get("role") in {"user", "assistant"}]
            return self._extract_entries_from_messages(
                messages=messages,
                session_id=path.stem,
                session_file=path.name,
                session_format="jsonl",
            )
        except Exception:
            return []
        return []

    def _extract_entries_from_messages(
        self,
        messages: list[dict],
        session_id: str,
        session_file: str,
        session_format: str,
    ) -> list[dict]:
        entries: list[dict] = []
        for index, message in enumerate(messages):
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            content = content.strip()
            if not content:
                continue
            direction = "inbound" if role == "user" else "outbound"
            message_timestamp = message.get("timestamp")
            entries.append(
                {
                    "key": f"{session_id}:{role}:{message_timestamp or ''}:{content}",
                    "direction": direction,
                    "content": content,
                    "session_id": session_id,
                    "session_file": session_file,
                    "session_format": session_format,
                    "message_index": index,
                    "message_timestamp": message_timestamp,
                }
            )
        return entries
