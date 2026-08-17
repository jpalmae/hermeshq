import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.events import EventAudience, EventBroker
from hermeshq.models.activity import ActivityLog
from hermeshq.services.gateway_types import GatewayProcessHandle
from hermeshq.services.hermes_installation import HermesInstallationManager

if TYPE_CHECKING:
    from hermeshq.models.messaging_channel import MessagingChannel

logger = logging.getLogger(__name__)


class GatewayLogManager:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_broker: EventBroker,
        installation_manager: HermesInstallationManager,
        processes: dict[str, GatewayProcessHandle],
    ) -> None:
        self.session_factory = session_factory
        self.event_broker = event_broker
        self.installation_manager = installation_manager
        self.processes = processes
        self._activity_key_cache: dict[str, tuple[float, set[str]]] = {}
        self._ACTIVITY_KEY_CACHE_TTL = 30  # seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def tail_log(self, agent_id: str, platform: str, lines: int = 120) -> str:
        # Enterprise gateways run as async tasks, not subprocesses — no file logs
        if platform in ("google_chat", "kapso_whatsapp"):
            return ""

        async with self.session_factory() as session:
            from hermeshq.models.agent import Agent

            agent = await session.get(Agent, agent_id)
            channel = await self._get_channel(session, agent_id, platform)
            if not channel or not agent:
                return ""
            log_path = self.gateway_log_path(agent.workspace_path)

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

    # ------------------------------------------------------------------
    # Activity sync loop
    # ------------------------------------------------------------------

    async def activity_sync_loop(
        self,
        agent_id: str,
        node_id: str | None,
        workspace_path: str,
        platform: str,
        known_activity_keys: set[str],
        session_file_state: dict[str, tuple[int, int, int]],
    ) -> None:
        sessions_dir = self.sessions_dir(workspace_path)
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
                created_task_ids: list[str] = []
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
                    created_task_ids = await self._create_gateway_tasks_in_session(
                        session, agent_id, platform, new_entries, existing_keys
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
                        },
                        audience=EventAudience.for_agent(agent_id),
                    )
                for task_id in created_task_ids:
                    await self.event_broker.publish(
                        {
                            "type": "task.completed",
                            "task_id": task_id,
                            "agent_id": agent_id,
                        },
                        audience=EventAudience.for_agent(agent_id),
                    )
            except asyncio.CancelledError:
                return
            except Exception:  # noqa: BLE001  # asyncio task — WebSocket stale
                continue

    # ------------------------------------------------------------------
    # Native gateway Task creation
    # ------------------------------------------------------------------

    _PLATFORM_LABELS: dict[str, str] = {
        "telegram": "Telegram",
        "whatsapp": "WhatsApp",
        "microsoft_teams": "Teams",
    }

    async def _create_gateway_tasks_in_session(
        self,
        session: AsyncSession,
        agent_id: str,
        platform: str,
        new_entries: list[dict],
        existing_keys: set[str],
    ) -> list[str]:
        """Create completed Task records for new inbound messages from native gateways.

        Runs inside an already-open session; caller must commit.
        Returns the list of created task IDs so the caller can publish events.
        """
        from hermeshq.models.base import utcnow
        from hermeshq.models.task import Task
        from hermeshq.services.task_board import next_board_order

        truly_new = [e for e in new_entries if e["key"] not in existing_keys]
        inbound_entries = [e for e in truly_new if e["direction"] == "inbound"]
        if not inbound_entries:
            return []

        # Build outbound lookup (session_id → list of outbound entries) for response pairing
        outbound_by_session: dict[str, list[dict]] = {}
        for e in truly_new:
            if e["direction"] == "outbound":
                outbound_by_session.setdefault(e["session_id"], []).append(e)

        label = self._PLATFORM_LABELS.get(platform, platform.replace("_", " ").title())
        created_ids: list[str] = []

        for entry in inbound_entries:
            prompt = entry["content"]
            session_id = entry["session_id"]

            # Pair with the first outbound message after this inbound in the same session
            response: str | None = None
            for outbound in outbound_by_session.get(session_id, []):
                if outbound["message_index"] > entry["message_index"]:
                    response = outbound["content"]
                    break

            title = prompt[:60] + ("…" if len(prompt) > 60 else "")
            task = Task(
                agent_id=agent_id,
                title=f"{label}: {title}",
                prompt=prompt,
                response=response,
                status="completed",
                board_column="done",
                board_order=next_board_order(),
                board_manual=False,
                completed_at=utcnow(),
                metadata_json={
                    "source": platform,
                    "platform": platform,
                    "gateway_source_key": entry["key"],
                    "session_id": session_id,
                    "native_gateway": True,
                },
            )
            session.add(task)
            await session.flush()
            created_ids.append(task.id)

        return created_ids

    # ------------------------------------------------------------------
    # Activity collection helpers
    # ------------------------------------------------------------------

    def snapshot_session_activity(
        self,
        sessions_dir: Path,
        platform: str,
    ) -> tuple[set[str], dict[str, tuple[int, int, int]]]:
        known_activity_keys: set[str] = set()
        session_file_state: dict[str, tuple[int, int, int]] = {}
        if not sessions_dir.exists():
            return known_activity_keys, session_file_state
        for path in sorted(sessions_dir.glob("*.jsonl")):
            if not path.is_file():
                continue
            stat = path.stat()
            entries, end_offset = self._read_session_entries(path, platform)
            session_file_state[path.as_posix()] = (stat.st_mtime_ns, stat.st_size, end_offset)
            for entry in entries:
                known_activity_keys.add(entry["key"])
        return known_activity_keys, session_file_state

    def _collect_new_session_activity(
        self,
        sessions_dir: Path,
        platform: str,
        known_activity_keys: set[str],
        session_file_state: dict[str, tuple[int, int, int]],
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
            prev = session_file_state.get(path.as_posix())
            if prev is not None and (prev[0], prev[1]) == fingerprint:
                continue

            # Determine read offset: resume from previous, or start from 0
            last_offset = 0
            if prev is not None:
                if prev[1] > stat.st_size:
                    # File shrank (truncated/rewritten), read from beginning
                    last_offset = 0
                else:
                    # File grew or stayed same size with changed mtime, read new bytes
                    last_offset = prev[2]

            entries, end_offset = self._read_session_entries(path, platform, last_offset)
            session_file_state[path.as_posix()] = (stat.st_mtime_ns, stat.st_size, end_offset)
            for entry in entries:
                if entry["key"] in known_activity_keys:
                    continue
                known_activity_keys.add(entry["key"])
                new_entries.append(entry)

        for tracked in list(session_file_state):
            if tracked not in current_files:
                session_file_state.pop(tracked, None)
        return new_entries

    def _read_session_entries(
        self,
        path: Path,
        platform: str,
        last_offset: int = 0,
    ) -> tuple[list[dict], int]:
        """Read new entries from a JSONL session file starting at *last_offset*.

        Returns ``(entries, new_offset)`` where *new_offset* is the byte
        position after the last byte read (i.e. ``st_size`` on success).
        """
        try:
            if path.suffix != ".jsonl":
                return [], last_offset
            file_size = path.stat().st_size
            if file_size == 0:
                return [], 0
            if file_size < last_offset:
                # File was truncated/rewritten — start from the beginning
                last_offset = 0
            if file_size == last_offset:
                return [], last_offset

            # Read only the new bytes appended since last_offset
            with open(path, encoding="utf-8") as f:
                f.seek(last_offset)
                new_text = f.read()
            new_offset = file_size

            lines = [line for line in new_text.splitlines() if line.strip()]
            if not lines:
                return [], new_offset
            payloads = []
            for line in lines:
                try:
                    payloads.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            # On the initial read (last_offset == 0) the first line is the
            # session header and carries the "platform" field — validate it.
            # On incremental reads the first new payload is a message record
            # without "platform", so we skip this guard to avoid silently
            # discarding all newly appended messages.
            if last_offset == 0 and (not payloads or payloads[0].get("platform") != platform):
                return [], new_offset
            messages = [item for item in payloads if item.get("role") in {"user", "assistant"}]
            entries = self._extract_entries_from_messages(
                messages=messages,
                session_id=path.stem,
                session_file=path.name,
                session_format="jsonl",
            )
            return entries, new_offset
        except OSError:
            return [], last_offset

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

    # ------------------------------------------------------------------
    # WhatsApp helpers
    # ------------------------------------------------------------------

    def _extract_whatsapp_qr_text(self, bridge_log_path: Path | None) -> str | None:
        if not bridge_log_path or not bridge_log_path.exists():
            return None
        try:
            lines = bridge_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
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

    def infer_whatsapp_pairing_status(
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
                tail = "\n".join(
                    bridge_log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
                ).lower()
            except OSError:
                tail = ""
            if "waiting for scan" in tail or "scan this qr code" in tail:
                return "waiting_scan"
        return "unpaired"

    # ------------------------------------------------------------------
    # Activity key cache helpers
    # ------------------------------------------------------------------

    async def _recent_activity_source_keys(self, session: AsyncSession, agent_id: str, platform: str) -> list[str]:
        cache_key = f"{agent_id}:{platform}"
        now = time.time()
        cached = self._activity_key_cache.get(cache_key)
        if cached and (now - cached[0]) < self._ACTIVITY_KEY_CACHE_TTL:
            return list(cached[1])
        result = await session.execute(
            select(ActivityLog.details)
            .where(
                ActivityLog.agent_id == agent_id,
                ActivityLog.event_type.in_((f"channel.{platform}.inbound", f"channel.{platform}.outbound")),
            )
            .order_by(desc(ActivityLog.created_at))
            .limit(200)
        )
        keys: list[str] = []
        for details in result.scalars():
            if isinstance(details, dict):
                source_key = details.get("source_key")
                if isinstance(source_key, str) and source_key:
                    keys.append(source_key)
        result_set = set(keys)
        self._activity_key_cache[cache_key] = (now, result_set)
        return keys

    # ------------------------------------------------------------------
    # Log file helpers
    # ------------------------------------------------------------------

    def cleanup_stale_gateway_pid(self, workspace_path: str) -> None:
        pid_path = self.installation_manager.build_hermes_home(workspace_path) / "gateway.pid"
        if not pid_path.exists():
            return
        try:
            payload = json.loads(pid_path.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            recorded_start = payload.get("start_time")
        except (json.JSONDecodeError, OSError, ValueError, KeyError):
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
        except (OSError, IndexError, ValueError):
            return
        if current_start != recorded_start:
            pid_path.unlink(missing_ok=True)
            return

        cmdline_path = proc_dir / "cmdline"
        try:
            cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip().lower()
        except OSError:
            return
        if "hermes" not in cmdline or "gateway" not in cmdline:
            pid_path.unlink(missing_ok=True)

    def _read_log_tail(self, path: Path, lines: int = 80) -> str:
        if not path.exists():
            return ""
        try:
            return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def gateway_log_path(self, workspace_path: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "logs" / "gateway.log"

    def sessions_dir(self, workspace_path: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "sessions"

    def _whatsapp_session_dir(self, workspace_path: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "whatsapp" / "session"

    def _whatsapp_bridge_log_path(self, workspace_path: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "whatsapp" / "bridge.log"

    # ------------------------------------------------------------------
    # Internal helpers (channel lookup used by tail_log)
    # ------------------------------------------------------------------

    async def _get_channel(self, session: AsyncSession, agent_id: str, platform: str) -> "MessagingChannel | None":
        from hermeshq.models.messaging_channel import MessagingChannel

        result = await session.execute(
            select(MessagingChannel).where(
                MessagingChannel.agent_id == agent_id,
                MessagingChannel.platform == platform,
            )
        )
        return result.scalar_one_or_none()
