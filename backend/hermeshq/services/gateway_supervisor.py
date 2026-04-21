import asyncio
import contextlib
import json
import logging
import subprocess
from dataclasses import dataclass
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


@dataclass
class GatewayProcessHandle:
    agent_id: str
    platform: str
    process: subprocess.Popen
    log_path: str
    log_handle: object
    monitor_task: asyncio.Task | None = None
    activity_task: asyncio.Task | None = None
    known_activity_keys: set[str] | None = None
    session_file_state: dict[str, tuple[int, int]] | None = None


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
        self.processes: dict[tuple[str, str], GatewayProcessHandle] = {}
        self._channel_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _get_channel_lock(self, agent_id: str, platform: str) -> asyncio.Lock:
        key = (agent_id, platform)
        lock = self._channel_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[key] = lock
        return lock

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
                else (
                    str((metadata.get("bootstrap") or {}).get("last_success_at") or "").strip() or None
                )
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
        semaphore = asyncio.Semaphore(BOOTSTRAP_CONCURRENCY)

        async def _bootstrap_one(channel: MessagingChannel, agent: Agent) -> None:
            if channel.platform != "telegram":
                return
            async with semaphore:
                attempt = 0
                while attempt < BOOTSTRAP_RETRY_ATTEMPTS:
                    attempt += 1
                    started_at = datetime.now(timezone.utc)
                    try:
                        await asyncio.wait_for(
                            self.start_channel(agent, channel.platform),
                            timeout=BOOTSTRAP_CHANNEL_TIMEOUT_SECONDS,
                        )
                        async with self.session_factory() as session:
                            session_channel = await self._get_channel(session, agent.id, channel.platform)
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
                            f"{channel.platform} gateway bootstrap timed out after "
                            f"{BOOTSTRAP_CHANNEL_TIMEOUT_SECONDS} seconds"
                        )
                        transient = True
                        log_event = f"channel.{channel.platform}.bootstrap_timeout"
                        log_message = f"{agent.name} {channel.platform} gateway bootstrap timed out"
                    except ValueError as exc:
                        error_text = str(exc)
                        transient = self._is_transient_bootstrap_error(error_text)
                        log_event = f"channel.{channel.platform}.bootstrap_failed"
                        log_message = f"{agent.name} {channel.platform} gateway bootstrap failed"
                    except Exception:
                        logger.exception(
                            "Unexpected gateway bootstrap failure for agent %s (%s)",
                            agent.id,
                            agent.name,
                        )
                        error_text = "unexpected_error"
                        transient = False
                        log_event = f"channel.{channel.platform}.bootstrap_failed"
                        log_message = f"{agent.name} {channel.platform} gateway bootstrap failed"

                    logger.warning(
                        "Telegram gateway bootstrap failed for agent %s (%s), attempt %s/%s: %s",
                        agent.id,
                        agent.name,
                        attempt,
                        BOOTSTRAP_RETRY_ATTEMPTS,
                        error_text,
                    )
                    async with self.session_factory() as session:
                        session_agent = await session.get(Agent, agent.id)
                        session_channel = await self._get_channel(session, agent.id, channel.platform)
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
                    await asyncio.sleep(BOOTSTRAP_RETRY_DELAYS_SECONDS[min(attempt - 1, len(BOOTSTRAP_RETRY_DELAYS_SECONDS) - 1)])

        await asyncio.gather(*(_bootstrap_one(channel, agent) for channel, agent in rows))

    async def shutdown(self) -> None:
        for agent_id, platform in list(self.processes):
            await self.stop_channel(agent_id, platform)

    async def get_runtime_status(self, agent_id: str, platform: str) -> dict:
        handle = self.processes.get((agent_id, platform))
        if handle and handle.process.poll() is None:
            async with self.session_factory() as session:
                channel = await self._get_channel(session, agent_id, platform)
                bootstrap = dict((channel.metadata_json or {}).get("bootstrap") or {}) if channel else {}
            return {
                "status": "running",
                "pid": handle.process.pid,
                "log_path": handle.log_path,
                "last_bootstrap_at": bootstrap.get("last_attempt_at"),
                "last_bootstrap_success_at": bootstrap.get("last_success_at"),
                "last_bootstrap_status": bootstrap.get("last_status"),
                "last_bootstrap_error": bootstrap.get("last_error"),
                "last_bootstrap_duration_ms": bootstrap.get("last_duration_ms"),
                "last_bootstrap_attempts": bootstrap.get("last_attempts"),
            }
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            channel = await self._get_channel(session, agent_id, platform)
            if not channel:
                return {"status": "missing", "pid": None, "log_path": None}
            bootstrap = dict((channel.metadata_json or {}).get("bootstrap") or {})
            return {
                "status": channel.status,
                "pid": None,
                "log_path": self._log_path(agent.workspace_path, platform).as_posix() if agent else None,
                "last_bootstrap_at": bootstrap.get("last_attempt_at"),
                "last_bootstrap_success_at": bootstrap.get("last_success_at"),
                "last_bootstrap_status": bootstrap.get("last_status"),
                "last_bootstrap_error": bootstrap.get("last_error"),
                "last_bootstrap_duration_ms": bootstrap.get("last_duration_ms"),
                "last_bootstrap_attempts": bootstrap.get("last_attempts"),
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
        key = (agent_id, platform)
        async with self._get_channel_lock(agent_id, platform):
            handle = self.processes.get(key)
            if handle and handle.process.poll() is None:
                return

            async with self.session_factory() as session:
                agent_row = await session.get(Agent, agent_id)
                channel = await self._get_channel(session, agent_id, platform)
                if not agent_row or not channel:
                    raise ValueError("Messaging channel not found")
                if not channel.enabled:
                    channel.status = "stopped"
                    await session.commit()
                    return
                if platform == "telegram" and not channel.secret_ref:
                    channel.status = "error"
                    channel.last_error = "Telegram bot token secret is required"
                    await self._log_channel_event(
                        session,
                        agent_row,
                        channel,
                        "channel.telegram.start_failed",
                        f"{agent_row.name} telegram gateway failed to start",
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
                            "channel.telegram.start_failed",
                            f"{agent_row.name} telegram gateway failed to start",
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
                        "channel.telegram.start_failed",
                        f"{agent_row.name} telegram gateway failed to start",
                        severity="warning",
                        details={"reason": "installation_sync_failed", "error": channel.last_error},
                    )
                    await session.commit()
                    raise ValueError(channel.last_error) from exc
                env = await self.installation_manager.build_gateway_env(agent_row, platform)
                runtime_selection = await self.installation_manager.resolve_hermes_runtime(agent_row)
                workspace_path = self.installation_manager.resolve_workspace_path(agent_row.workspace_path)
                log_path = self._log_path(agent_row.workspace_path, platform)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_handle = log_path.open("a", encoding="utf-8")
                process = subprocess.Popen(
                    [runtime_selection.hermes_bin, "gateway", "run"],
                    cwd=str(workspace_path),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
                channel.status = "running"
                channel.last_error = None
                channel.updated_at = utcnow()
                session.add(
                    ActivityLog(
                        agent_id=agent_row.id,
                        node_id=agent_row.node_id,
                        event_type=f"channel.{platform}.started",
                        message=f"{agent_row.name} {platform} gateway started",
                        details={"platform": platform, "pid": process.pid},
                    )
                )
                await session.commit()

            sessions_dir = self._sessions_dir(agent_row.workspace_path)
            known_activity_keys, session_file_state = await asyncio.to_thread(self._snapshot_session_activity, sessions_dir)
            monitor = asyncio.create_task(self._monitor_process(agent_id, platform, process, log_path.as_posix(), log_handle))
            activity_task = asyncio.create_task(
                self._activity_sync_loop(
                    agent_row.id,
                    agent_row.node_id,
                    str(workspace_path),
                    platform,
                    known_activity_keys,
                    session_file_state,
                )
            )
            self.processes[key] = GatewayProcessHandle(
                agent_id=agent_id,
                platform=platform,
                process=process,
                log_path=log_path.as_posix(),
                log_handle=log_handle,
                monitor_task=monitor,
                activity_task=activity_task,
                known_activity_keys=known_activity_keys,
                session_file_state=session_file_state,
            )
            await self.event_broker.publish(
                {"type": "messaging.status_changed", "agent_id": agent_id, "status": "running", "message": platform}
            )

    async def stop_channel(self, agent_id: str, platform: str) -> None:
        key = (agent_id, platform)
        async with self._get_channel_lock(agent_id, platform):
            handle = self.processes.pop(key, None)
            if handle:
                if handle.monitor_task:
                    handle.monitor_task.cancel()
                if handle.activity_task:
                    handle.activity_task.cancel()
                if handle.process.poll() is None:
                    handle.process.terminate()
                    try:
                        await asyncio.wait_for(asyncio.to_thread(handle.process.wait), timeout=5)
                    except asyncio.TimeoutError:
                        handle.process.kill()
                        await asyncio.to_thread(handle.process.wait)
                with contextlib.suppress(Exception):
                    handle.log_handle.close()

            async with self.session_factory() as session:
                agent = await session.get(Agent, agent_id)
                channel = await self._get_channel(session, agent_id, platform)
                if channel:
                    channel.status = "stopped"
                    channel.last_error = None
                if agent:
                    session.add(
                        ActivityLog(
                            agent_id=agent.id,
                            node_id=agent.node_id,
                            event_type=f"channel.{platform}.stopped",
                            message=f"{agent.name} {platform} gateway stopped",
                            details={"platform": platform},
                        )
                    )
                await session.commit()
            await self.event_broker.publish(
                {"type": "messaging.status_changed", "agent_id": agent_id, "status": "stopped", "message": platform}
            )

    async def restart_channel(self, agent_id: str, platform: str) -> None:
        await self.stop_channel(agent_id, platform)
        await self.start_channel(agent_id, platform)

    async def tail_log(self, agent_id: str, platform: str, lines: int = 120) -> str:
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            channel = await self._get_channel(session, agent_id, platform)
            if not channel:
                return ""
            log_path = self._log_path(agent.workspace_path, platform) if agent else None
        if not log_path:
            return ""
        if not log_path.exists():
            return ""
        content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])

    async def _monitor_process(
        self,
        agent_id: str,
        platform: str,
        process: subprocess.Popen,
        log_path: str,
        log_handle,
    ) -> None:
        try:
            return_code = await asyncio.to_thread(process.wait)
        except asyncio.CancelledError:
            return
        finally:
            with contextlib.suppress(Exception):
                log_handle.flush()
                log_handle.close()

        handle = self.processes.pop((agent_id, platform), None)
        if handle and handle.activity_task:
            handle.activity_task.cancel()
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            channel = await self._get_channel(session, agent_id, platform)
            if not channel:
                return
            channel.status = "stopped" if return_code == 0 else "error"
            channel.last_error = None if return_code == 0 else f"{platform} gateway exited with code {return_code}"
            if agent:
                session.add(
                    ActivityLog(
                        agent_id=agent.id,
                        node_id=agent.node_id,
                        event_type=f"channel.{platform}.exited",
                        message=f"{agent.name} {platform} gateway exited",
                        details={"platform": platform, "return_code": return_code, "log_path": log_path},
                    )
                )
            await session.commit()
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
        if platform != "telegram":
            return

        sessions_dir = self._sessions_dir(workspace_path)
        while True:
            try:
                await asyncio.sleep(5)
                new_entries = await asyncio.to_thread(
                    self._collect_new_session_activity,
                    sessions_dir,
                    known_activity_keys,
                    session_file_state,
                )
                if not new_entries:
                    continue
                async with self.session_factory() as session:
                    existing_keys = {
                        source_key
                        for source_key in await self._recent_activity_source_keys(session, agent_id)
                        if source_key
                    }
                    for entry in new_entries:
                        if entry["key"] in existing_keys:
                            continue
                        session.add(
                            ActivityLog(
                                agent_id=agent_id,
                                node_id=node_id,
                                event_type=f"channel.telegram.{entry['direction']}",
                                severity="info",
                                message=entry["content"],
                                details={
                                    "platform": "telegram",
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
                            "platform": "telegram",
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

    async def _recent_activity_source_keys(self, session: AsyncSession, agent_id: str) -> list[str]:
        result = await session.execute(
            select(ActivityLog.details)
            .where(
                ActivityLog.agent_id == agent_id,
                ActivityLog.event_type.in_(("channel.telegram.inbound", "channel.telegram.outbound")),
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

    def _log_path(self, workspace_path: str, platform: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "logs" / f"{platform}-gateway.log"

    def _sessions_dir(self, workspace_path: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "sessions"

    def _snapshot_session_activity(self, sessions_dir: Path) -> tuple[set[str], dict[str, tuple[int, int]]]:
        known_activity_keys: set[str] = set()
        session_file_state: dict[str, tuple[int, int]] = {}
        if not sessions_dir.exists():
            return known_activity_keys, session_file_state
        for path in sorted(sessions_dir.glob("*.jsonl")):
            if not path.is_file():
                continue
            stat = path.stat()
            session_file_state[path.as_posix()] = (stat.st_mtime_ns, stat.st_size)
            for entry in self._read_telegram_session_entries(path):
                known_activity_keys.add(entry["key"])
        return known_activity_keys, session_file_state

    def _collect_new_session_activity(
        self,
        sessions_dir: Path,
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
            for entry in self._read_telegram_session_entries(path):
                if entry["key"] in known_activity_keys:
                    continue
                known_activity_keys.add(entry["key"])
                new_entries.append(entry)

        for tracked in list(session_file_state):
            if tracked not in current_files:
                session_file_state.pop(tracked, None)
        return new_entries

    def _read_telegram_session_entries(self, path: Path) -> list[dict]:
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
            if not payloads or payloads[0].get("platform") != "telegram":
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
