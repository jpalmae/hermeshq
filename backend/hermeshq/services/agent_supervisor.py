import asyncio
import traceback
from typing import Any

from telegram import Bot
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.events import EventBroker
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.message import AgentMessage
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.node import Node
from hermeshq.models.secret import Secret
from hermeshq.models.task import Task
from hermeshq.models.base import utcnow
from hermeshq.services.hermes_runtime import HermesRuntime
from hermeshq.services.secret_vault import SecretVault


class AgentSupervisor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_broker: EventBroker,
        runtime: HermesRuntime,
        secret_vault: SecretVault,
    ) -> None:
        self.session_factory = session_factory
        self.event_broker = event_broker
        self.runtime = runtime
        self.secret_vault = secret_vault
        self.running_agents: set[str] = set()
        self.active_tasks: dict[str, asyncio.Task] = {}
        self._pending_callbacks: list = []

    async def bootstrap_runtime(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(select(Agent).where(Agent.status == "running"))
            for agent in result.scalars().all():
                self.running_agents.add(agent.id)

    async def start_agent(self, agent_id: str) -> Agent:
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                raise ValueError("Agent not found")
            agent.status = "running"
            agent.last_activity = utcnow()
            self.running_agents.add(agent.id)
            await self._log(session, "agent.started", agent=agent, message=f"{agent.name} started")
            await session.commit()
            await session.refresh(agent)
        await self.event_broker.publish(
            {
                "type": "agent.status_changed",
                "agent_id": agent_id,
                "status": "running",
            }
        )
        await self._start_pending_tasks(agent_id)
        return agent

    async def stop_agent(self, agent_id: str) -> Agent:
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                raise ValueError("Agent not found")
            agent.status = "stopped"
            agent.last_activity = utcnow()
            self.running_agents.discard(agent.id)
            await self._log(session, "agent.stopped", agent=agent, message=f"{agent.name} stopped")
            await session.commit()
            await session.refresh(agent)
        await self.event_broker.publish(
            {
                "type": "agent.status_changed",
                "agent_id": agent_id,
                "status": "stopped",
            }
        )
        return agent

    async def restart_agent(self, agent_id: str) -> Agent:
        await self.stop_agent(agent_id)
        return await self.start_agent(agent_id)

    async def submit_task(self, task_id: str) -> None:
        if task_id in self.active_tasks:
            return
        runner = asyncio.create_task(self._run_task(task_id))
        self.active_tasks[task_id] = runner

    async def cancel_task(self, task_id: str) -> None:
        runner = self.active_tasks.get(task_id)
        if runner:
            runner.cancel()

    async def _start_pending_tasks(self, agent_id: str) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Task)
                .where(Task.agent_id == agent_id, Task.status == "queued")
                .order_by(Task.queued_at.asc())
            )
            queued_tasks = result.scalars().all()
        for task in queued_tasks:
            await self.submit_task(task.id)

    async def _run_task(self, task_id: str) -> None:
        try:
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                if not task:
                    return
                agent = await session.get(Agent, task.agent_id)
                if not agent:
                    return
                if agent.status != "running":
                    return
                task.status = "running"
                task.started_at = utcnow()
                task.messages_json = []
                task.tool_calls = []
                await self._log(
                    session,
                    "task.started",
                    agent=agent,
                    task=task,
                    message=task.title or task.prompt[:72],
                )
                await session.commit()

            await self.event_broker.publish(
                {
                    "type": "task.started",
                    "task_id": task_id,
                    "agent_id": task.agent_id,
                }
            )

            async def stream_callback(delta: str, index: int | None = None) -> None:
                async with self.session_factory() as inner_session:
                    task_row = await inner_session.get(Task, task_id)
                    agent_row = await inner_session.get(Agent, task_row.agent_id) if task_row else None
                    if not task_row or not agent_row:
                        return
                    task_row.messages_json = [
                        *task_row.messages_json,
                        {"role": "assistant", "content": delta},
                    ]
                    if index is not None:
                        task_row.iterations = max(task_row.iterations, index)
                    await self._log(
                        inner_session,
                        "agent.output",
                        agent=agent_row,
                        task=task_row,
                        message=delta[:240],
                        details={"step": index}
                        if index is not None
                        else {"engine": "hermes-agent" if self.runtime.available else "simulated"},
                    )
                    agent_row.last_activity = utcnow()
                    await inner_session.commit()
                await self.event_broker.publish(
                    {
                        "type": "task.progress",
                        "task_id": task_id,
                        "agent_id": task.agent_id,
                        "message": delta,
                        "step": index,
                    }
                )

            execution = await self.runtime.execute(agent, task, stream_callback)
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                agent = await session.get(Agent, task.agent_id) if task else None
                if not task or not agent:
                    return
                task.status = "completed"
                task.completed_at = utcnow()
                task.response = execution.final_response
                task.tokens_used = execution.tokens_used
                task.iterations = max(task.iterations, execution.iterations)
                task.messages_json = execution.messages or task.messages_json
                task.tool_calls = execution.tool_calls
                agent.total_tasks += 1
                agent.total_tokens_used += task.tokens_used
                agent.last_activity = utcnow()
                await self._log(
                    session,
                    "task.completed",
                    agent=agent,
                    task=task,
                    message=task.title or "Task completed",
                        details={"tokens_used": task.tokens_used, "engine": execution.engine},
                )
                await self._queue_delegate_result_callback(
                    session,
                    task=task,
                    agent=agent,
                    success=True,
                    summary=execution.final_response,
                )
                await self._queue_external_callback_delivery(
                    session,
                    task=task,
                    agent=agent,
                    success=True,
                    summary=execution.final_response,
                )
                await session.commit()
                await self._drain_pending_callbacks()

            await self.event_broker.publish(
                {
                    "type": "task.completed",
                    "task_id": task_id,
                    "agent_id": task.agent_id,
                    "response": execution.final_response,
                }
            )
        except asyncio.CancelledError:
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                agent = await session.get(Agent, task.agent_id) if task else None
                if task:
                    task.status = "cancelled"
                    task.completed_at = utcnow()
                if agent:
                    agent.last_activity = utcnow()
                    await self._log(
                        session,
                        "task.cancelled",
                        agent=agent,
                        task=task,
                        message=task.title or "Task cancelled",
                    )
                await session.commit()
            await self.event_broker.publish({"type": "task.cancelled", "task_id": task_id})
        except Exception as exc:
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                agent = await session.get(Agent, task.agent_id) if task else None
                if task:
                    task.status = "failed"
                    task.completed_at = utcnow()
                    task.error_message = str(exc)
                if agent:
                    agent.last_activity = utcnow()
                    await self._log(
                        session,
                        "task.failed",
                        agent=agent,
                        task=task,
                        message=task.title or "Task failed",
                        details={
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                            "traceback": traceback.format_exc(),
                        },
                    )
                    await self._queue_delegate_result_callback(
                        session,
                        task=task,
                        agent=agent,
                        success=False,
                        summary=str(exc),
                    )
                    await self._queue_external_callback_delivery(
                        session,
                        task=task,
                        agent=agent,
                        success=False,
                        summary=str(exc),
                    )
                await session.commit()
                await self._drain_pending_callbacks()
            await self.event_broker.publish(
                {
                    "type": "task.failed",
                    "task_id": task_id,
                    "agent_id": task.agent_id if task else None,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            )
        finally:
            self.active_tasks.pop(task_id, None)

    async def _log(
        self,
        session: AsyncSession,
        event_type: str,
        *,
        agent: Agent | None = None,
        task: Task | None = None,
        node: Node | None = None,
        message: str | None = None,
        details: dict | None = None,
    ) -> None:
        session.add(
            ActivityLog(
                agent_id=agent.id if agent else None,
                task_id=task.id if task else None,
                node_id=node.id if node else agent.node_id if agent else None,
                event_type=event_type,
                message=message,
                details=details or {},
            )
        )

    async def _queue_delegate_result_callback(
        self,
        session: AsyncSession,
        *,
        task: Task,
        agent: Agent,
        success: bool,
        summary: str,
    ) -> None:
        if not task.source_agent_id:
            return
        if (task.metadata_json or {}).get("delegation_result"):
            return

        source_agent = await session.get(Agent, task.source_agent_id)
        if not source_agent:
            return

        status_label = "completed" if success else "failed"
        child_name = agent.friendly_name or agent.name or agent.slug or agent.id
        source_name = source_agent.friendly_name or source_agent.name or source_agent.slug or source_agent.id
        title = f"Delegation result from {child_name}"
        message_content = (
            f"Delegated task update from {child_name}: {status_label}.\n\n"
            f"Original instruction:\n{task.prompt}\n\n"
            f"Result:\n{summary.strip() or '(no response)'}"
        )

        callback_message = AgentMessage(
            from_agent_id=agent.id,
            to_agent_id=source_agent.id,
            task_id=task.id,
            message_type="delegate_result",
            content=message_content,
            metadata_json={
                "delegated_result": True,
                "status": status_label,
                "source_task_id": task.id,
                "parent_task_id": task.parent_task_id,
            },
        )
        session.add(callback_message)

        callback_task = Task(
            agent_id=source_agent.id,
            source_agent_id=agent.id,
            parent_task_id=task.parent_task_id,
            title=title,
            prompt=(
                f"A delegated task you assigned to {child_name} has {status_label}.\n\n"
                f"Original delegated instruction:\n{task.prompt}\n\n"
                f"{child_name} result:\n{summary.strip() or '(no response)'}\n\n"
                "If needed, continue the orchestration and inform the user."
            ),
            metadata_json={
                "delegation_result": True,
                "delegated_task_id": task.id,
                "delegated_agent_id": agent.id,
                "delegated_agent_name": child_name,
                "status": status_label,
                "callback_delivery": (task.metadata_json or {}).get("callback_delivery"),
            },
        )
        session.add(callback_task)
        await session.flush()
        callback_message.task_id = callback_task.id

        await self._log(
            session,
            "comms.delegate_result",
            agent=source_agent,
            task=callback_task,
            message=f"{child_name} -> {source_name}: delegated task {status_label}",
            details={
                "delegated_task_id": task.id,
                "delegated_agent_id": agent.id,
                "status": status_label,
            },
        )

        async def _after_commit() -> None:
            if source_agent.status == "running":
                await self.submit_task(callback_task.id)
            await self.event_broker.publish(
                {
                    "type": "comms.message",
                    "message_id": callback_message.id,
                    "from_agent_id": callback_message.from_agent_id,
                    "to_agent_id": callback_message.to_agent_id,
                    "message_type": callback_message.message_type,
                    "content": callback_message.content,
                    "task_id": callback_task.id,
                }
            )
            pty_manager = getattr(self, "pty_manager", None)
            if pty_manager is not None:
                notice = (
                    f"\r\n[HermesHQ] Delegation result from {child_name}: {status_label}. "
                    f"Task {task.id}\r\n"
                )
                await pty_manager.broadcast_notice(source_agent.id, notice)

        self._pending_callbacks.append(_after_commit)

    async def _queue_external_callback_delivery(
        self,
        session: AsyncSession,
        *,
        task: Task,
        agent: Agent,
        success: bool,
        summary: str,
    ) -> None:
        metadata = task.metadata_json or {}
        callback_delivery = metadata.get("callback_delivery")
        if not isinstance(callback_delivery, dict):
            return
        platform = str(callback_delivery.get("platform") or "").strip().lower()
        chat_id = str(callback_delivery.get("chat_id") or "").strip()
        thread_id = callback_delivery.get("thread_id")
        if platform != "telegram" or not chat_id:
            return

        message_text = summary.strip() if success else f"Delegated task failed: {summary.strip()}"
        if not message_text:
            return
        source_agent = await session.get(Agent, task.agent_id)
        if not source_agent:
            return
        result = await session.execute(
            select(MessagingChannel).where(
                MessagingChannel.agent_id == source_agent.id,
                MessagingChannel.platform == "telegram",
                MessagingChannel.enabled.is_(True),
            )
        )
        channel = result.scalar_one_or_none()
        if not channel or not channel.secret_ref:
            return
        secret_result = await session.execute(select(Secret).where(Secret.name == channel.secret_ref))
        secret = secret_result.scalar_one_or_none()
        if not secret:
            return
        token = self.secret_vault.decrypt(secret.value_enc)
        thread_value = int(str(thread_id)) if thread_id not in (None, "", "None") else None

        async def _after_commit() -> None:
            try:
                bot = Bot(token=token)
                await bot.send_message(chat_id=chat_id, text=message_text, message_thread_id=thread_value)
                await bot.shutdown()
            except Exception:
                pass

        self._pending_callbacks.append(_after_commit)

    async def _drain_pending_callbacks(self) -> None:
        if not self._pending_callbacks:
            return
        callbacks = list(self._pending_callbacks)
        self._pending_callbacks.clear()
        for callback in callbacks:
            await callback()

    async def get_recent_activity(self, limit: int = 20) -> list[ActivityLog]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ActivityLog).order_by(desc(ActivityLog.created_at)).limit(limit)
            )
            return list(result.scalars().all())
