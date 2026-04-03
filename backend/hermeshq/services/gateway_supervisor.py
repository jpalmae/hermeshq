import asyncio
import contextlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.events import EventBroker
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.base import utcnow
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.services.hermes_installation import HermesInstallationManager


@dataclass
class GatewayProcessHandle:
    agent_id: str
    platform: str
    process: subprocess.Popen
    log_path: str
    log_handle: object
    monitor_task: asyncio.Task | None = None


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

    async def bootstrap_gateways(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(MessagingChannel, Agent)
                .join(Agent, Agent.id == MessagingChannel.agent_id)
                .where(MessagingChannel.enabled.is_(True))
            )
            rows = result.all()
        for channel, agent in rows:
            if channel.platform != "telegram":
                continue
            await self.start_channel(agent, channel.platform)

    async def shutdown(self) -> None:
        for agent_id, platform in list(self.processes):
            await self.stop_channel(agent_id, platform)

    async def get_runtime_status(self, agent_id: str, platform: str) -> dict:
        handle = self.processes.get((agent_id, platform))
        if handle and handle.process.poll() is None:
            return {"status": "running", "pid": handle.process.pid, "log_path": handle.log_path}
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            channel = await self._get_channel(session, agent_id, platform)
            if not channel:
                return {"status": "missing", "pid": None, "log_path": None}
            return {
                "status": channel.status,
                "pid": None,
                "log_path": self._log_path(agent.workspace_path, platform).as_posix() if agent else None,
            }

    async def start_channel(self, agent: Agent | str, platform: str) -> None:
        agent_id = agent.id if isinstance(agent, Agent) else agent
        key = (agent_id, platform)
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
                await session.commit()
                raise ValueError(channel.last_error)
            await self.installation_manager.sync_agent_installation(agent_row)
            env = await self.installation_manager.build_gateway_env(agent_row, platform)
            log_path = self._log_path(agent_row.workspace_path, platform)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("a", encoding="utf-8")
            process = subprocess.Popen(
                ["hermes", "gateway", "run"],
                cwd=agent_row.workspace_path,
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

        monitor = asyncio.create_task(self._monitor_process(agent_id, platform, process, log_path.as_posix(), log_handle))
        self.processes[key] = GatewayProcessHandle(
            agent_id=agent_id,
            platform=platform,
            process=process,
            log_path=log_path.as_posix(),
            log_handle=log_handle,
            monitor_task=monitor,
        )
        await self.event_broker.publish(
            {"type": "messaging.status_changed", "agent_id": agent_id, "status": "running", "message": platform}
        )

    async def stop_channel(self, agent_id: str, platform: str) -> None:
        key = (agent_id, platform)
        handle = self.processes.pop(key, None)
        if handle:
            if handle.monitor_task:
                handle.monitor_task.cancel()
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

        self.processes.pop((agent_id, platform), None)
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

    async def _get_channel(self, session: AsyncSession, agent_id: str, platform: str) -> MessagingChannel | None:
        result = await session.execute(
            select(MessagingChannel).where(
                MessagingChannel.agent_id == agent_id,
                MessagingChannel.platform == platform,
            )
        )
        return result.scalar_one_or_none()

    def _log_path(self, workspace_path: str, platform: str) -> Path:
        return self.installation_manager.build_hermes_home(workspace_path) / "logs" / f"{platform}-gateway.log"
