from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Bot

logger = logging.getLogger(__name__)


class _StreamBuffer:
    """Accumulates streaming deltas and flushes them to the DB in batches."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        task_id: str,
        agent_id: str,
        log_func,
        event_broker: EventBroker,
        flush_interval: float = 0.5,
        user_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._task_id = task_id
        self._agent_id = agent_id
        self._user_id = user_id
        self._log_func = log_func
        self._event_broker = event_broker
        self._flush_interval = flush_interval
        self._deltas: list[tuple[str, int | None]] = []
        self._flush_task: asyncio.Task | None = None

    # -- public API ----------------------------------------------------------

    def start_flush_loop(self) -> None:
        """Start the periodic flush background task."""
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop_flush_loop(self) -> None:
        """Cancel the periodic flush and drain remaining deltas."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        # Final drain of any remaining buffered deltas.
        await self.flush()

    async def push(self, delta: str, index: int | None = None) -> None:
        """Append a delta to the buffer and publish an event immediately."""
        self._deltas.append((delta, index))
        event = {
            "type": "task.progress",
            "task_id": self._task_id,
            "agent_id": self._agent_id,
            "message": delta,
            "step": index,
        }
        if self._user_id:
            event["created_by_user_id"] = self._user_id
        await self._event_broker.publish(event)

    async def flush(self) -> None:
        """Write all buffered deltas to the DB in a single transaction."""
        if not self._deltas:
            return

        snapshots = self._deltas[:]
        self._deltas.clear()

        # Collapse deltas into a single content string per batch so that
        # messages_json grows by *one* entry per flush instead of N.
        combined_content = "".join(d for d, _ in snapshots)
        max_index: int | None = None
        for _, idx in snapshots:
            if idx is not None:
                max_index = idx if max_index is None else max(max_index, idx)

        async with self._session_factory() as session:
            task_row = await session.get(Task, self._task_id)
            agent_row = (
                await session.get(Agent, task_row.agent_id) if task_row else None
            )
            if not task_row or not agent_row:
                return

            task_row.messages_json = [
                *task_row.messages_json,
                {"role": "assistant", "content": combined_content},
            ]
            if max_index is not None:
                task_row.iterations = max(task_row.iterations, max_index)
            await self._log_func(
                session,
                "agent.output",
                agent=agent_row,
                task=task_row,
                message=combined_content[:240],
                details=(
                    {"step": max_index}
                    if max_index is not None
                    else {
                        "engine": "hermes-agent",
                        "batch_size": len(snapshots),
                    }
                ),
            )
            agent_row.last_activity = utcnow()
            await session.commit()

    # -- internals -----------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Periodic background flush."""
        try:
            while True:
                await asyncio.sleep(self._flush_interval)
                try:
                    await self.flush()
                except Exception:  # noqa: BLE001  # periodic flush — any error logged
                    logger.exception("StreamBuffer periodic flush error")
        except asyncio.CancelledError:
            return

from hermeshq.config import get_settings
from hermeshq.core.events import EventBroker
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.base import utcnow
from hermeshq.models.message import AgentMessage
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.node import Node
from hermeshq.models.secret import Secret
from hermeshq.models.task import Task
from hermeshq.services.hermes_runtime import HermesRuntime
from hermeshq.services.secret_vault import SecretVault
from hermeshq.services.task_board import runtime_status_to_board_column, sync_board_with_runtime


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
        settings = get_settings()
        self._concurrency_semaphore = asyncio.Semaphore(settings.concurrency_semaphore)
        self._semaphore_value = settings.concurrency_semaphore

    def update_semaphore(self, new_value: int) -> None:
        """Update the concurrency semaphore at runtime.

        Creates a new semaphore with the given value. Tasks currently
        waiting on the old semaphore will continue waiting; new tasks
        will use the new one.  This is safe to call from any thread.
        """
        self._semaphore_value = new_value
        self._concurrency_semaphore = asyncio.Semaphore(new_value)

    def _build_conversation_assistant_content(self, task: Task) -> str:
        if task.response and task.response.strip():
            return task.response.strip()
        streamed = "".join(
            str(message.get("content") or "")
            for message in (task.messages_json or [])
            if message.get("role") == "assistant"
        ).strip()
        if streamed:
            return streamed
        if task.status == "failed" and task.error_message:
            return task.error_message.strip()
        return ""

    async def _build_conversation_history(self, session: AsyncSession, task: Task) -> list[dict]:
        metadata = task.metadata_json or {}
        if not metadata.get("conversation"):
            return []
        thread_id = str(metadata.get("thread_id") or "").strip()

        result = await session.execute(
            select(Task)
            .where(Task.agent_id == task.agent_id)
            .order_by(desc(Task.queued_at))
            .limit(24)
        )
        candidates = list(result.scalars().all())
        prior_turns = [
            item
            for item in reversed(candidates)
            if item.id != task.id
            and (item.metadata_json or {}).get("conversation")
            and (
                not thread_id
                or str((item.metadata_json or {}).get("thread_id") or "").strip() == thread_id
            )
        ]
        history: list[dict] = []
        for prior in prior_turns[-6:]:
            if prior.prompt.strip():
                history.append({"role": "user", "content": prior.prompt.strip()})
            assistant_content = self._build_conversation_assistant_content(prior)
            if assistant_content:
                history.append({"role": "assistant", "content": assistant_content})
        return history

    async def bootstrap_runtime(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(select(Agent).where(Agent.status == "running"))
            for agent in result.scalars().all():
                self.running_agents.add(agent.id)

        # ── Recover zombie tasks left from previous container lifecycle ──
        # When the backend container restarts, all subprocess tasks die
        # without updating the DB. Mark them as failed so they don't stay
        # stuck in "running" forever.
        await self._recover_zombie_tasks()

    async def _recover_zombie_tasks(self) -> None:
        """Mark tasks stuck in 'running' or 'queued' as failed after a restart."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(Task).where(
                    Task.status.in_(["running", "queued"]),
                )
            )
            zombies = result.scalars().all()
            if not zombies:
                return

            now = utcnow()
            for task in zombies:
                prev_status = task.status
                task.status = "failed"
                task.error_message = (
                    f"Task was {prev_status} when the server restarted "
                    "and could not be resumed."
                )
                task.completed_at = now
                if not task.board_manual:
                    task.board_column = "failed"

            await session.commit()

            logging.getLogger(__name__).warning(
                "Recovered %d zombie tasks — marked as failed",
                len(zombies),
            )

    async def start_agent(self, agent_id: str) -> Agent:
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if not agent:
                raise ValueError("Agent not found")
            if agent.is_archived:
                raise ValueError("Archived agents cannot be started")
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
        runner = asyncio.create_task(self._run_task_with_semaphore(task_id))
        self.active_tasks[task_id] = runner

    async def _run_task_with_semaphore(self, task_id: str) -> None:
        async with self._concurrency_semaphore:
            await self._run_task(task_id)

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
            conversation_history: list[dict] = []
            session_id: str | None = None
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                if not task:
                    return
                if task.status != "queued":
                    return
                agent = await session.get(Agent, task.agent_id)
                if not agent:
                    return
                if agent.status != "running":
                    task.status = "failed"
                    await session.commit()
                    event = {
                        "type": "task.failed",
                        "task_id": task_id,
                        "agent_id": task.agent_id,
                        "error": f"Agent is not running (status={agent.status})",
                        "error_type": "AgentNotRunning",
                    }
                    if task.created_by_user_id:
                        event["created_by_user_id"] = task.created_by_user_id
                    await self.event_broker.publish(event)
                    return
                task_owner_id = task.created_by_user_id
                conversation_history = await self._build_conversation_history(session, task)
                metadata = task.metadata_json or {}
                if metadata.get("conversation"):
                    candidate_session_id = str(metadata.get("thread_id") or "").strip()
                    if candidate_session_id:
                        session_id = candidate_session_id
                task.status = "running"
                await sync_board_with_runtime(session, task.id, task.status)
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

            event = {
                "type": "task.started",
                "task_id": task_id,
                "agent_id": task.agent_id,
            }
            if task_owner_id:
                event["created_by_user_id"] = task_owner_id
            await self.event_broker.publish(event)

            stream_buffer = _StreamBuffer(
                session_factory=self.session_factory,
                task_id=task_id,
                agent_id=task.agent_id,
                log_func=self._log,
                event_broker=self.event_broker,
                user_id=task_owner_id,
            )
            stream_buffer.start_flush_loop()

            async def stream_callback(delta: str, index: int | None = None) -> None:
                await stream_buffer.push(delta, index)

            try:
                execution = await self.runtime.execute(
                    agent,
                    task,
                    stream_callback,
                    conversation_history=conversation_history,
                    session_id=session_id,
                )
            finally:
                # Ensure every buffered delta is persisted before proceeding.
                await stream_buffer.stop_flush_loop()
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                agent = await session.get(Agent, task.agent_id) if task else None
                if not task or not agent:
                    return
                task.status = "completed"
                await sync_board_with_runtime(session, task.id, task.status)
                task.completed_at = utcnow()
                task.response = execution.final_response
                task.tokens_used = execution.tokens_used
                task.iterations = max(task.iterations, execution.iterations)
                task.messages_json = execution.messages or task.messages_json
                task.tool_calls = execution.tool_calls

                # ── Persist response_attachments in task metadata ──
                response_attachments = getattr(execution, "response_attachments", [])
                if response_attachments:
                    metadata = dict(task.metadata_json or {})
                    # Strip source_path (internal only, never sent to client)
                    metadata["response_attachments"] = [
                        {k: v for k, v in att.items() if k != "source_path"}
                        for att in response_attachments
                    ]
                    task.metadata_json = metadata

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

            completed_event = {
                "type": "task.completed",
                "task_id": task_id,
                "agent_id": task.agent_id,
                "response": execution.final_response,
                "metadata": task.metadata_json or {},
            }
            if task_owner_id:
                completed_event["created_by_user_id"] = task_owner_id
            await self.event_broker.publish(completed_event)

            # Post-task hooks
            await self._run_post_task_hooks(task_id)

        except asyncio.CancelledError:
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                agent = await session.get(Agent, task.agent_id) if task else None
                if task:
                    task.status = "cancelled"
                    await sync_board_with_runtime(session, task.id, task.status)
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
            cancelled_event: dict = {"type": "task.cancelled", "task_id": task_id}
            if task and task.created_by_user_id:
                cancelled_event["created_by_user_id"] = task.created_by_user_id
            await self.event_broker.publish(cancelled_event)
        except Exception as exc:  # noqa: BLE001  # task cancellation catch-all
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                agent = await session.get(Agent, task.agent_id) if task else None
                if task:
                    task.status = "failed"
                    await sync_board_with_runtime(session, task.id, task.status)
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
            failed_event = {
                "type": "task.failed",
                "task_id": task_id,
                "agent_id": task.agent_id if task else None,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
            if task and task.created_by_user_id:
                failed_event["created_by_user_id"] = task.created_by_user_id
            await self.event_broker.publish(failed_event)
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

        # ── Channel routing: skip external delivery for mobile_app tasks ──
        reply_to = str(metadata.get("reply_to") or metadata.get("source") or "").strip().lower()
        if reply_to == "mobile_app":
            return

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
            except (httpx.HTTPError, RuntimeError):
                logger.warning("Failed to send Telegram notification to chat %s", chat_id, exc_info=True)

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

    # ------------------------------------------------------------------
    # Post-task hooks
    # ------------------------------------------------------------------

    async def _run_post_task_hooks(self, task_id: str) -> None:
        """Run post-completion hooks based on task metadata."""
        try:
            async with self.session_factory() as session:
                task = await session.get(Task, task_id)
                if not task:
                    return
                metadata = task.metadata_json or {}

                # Avatar generation hook
                if metadata.get("avatar_generation"):
                    target_agent_id = metadata.get("target_agent_id")
                    if target_agent_id:
                        await self._apply_avatar_generation(session, task, target_agent_id)
        except Exception as exc:  # noqa: BLE001  # post-task hook best-effort
            import logging
            logging.getLogger(__name__).warning("Post-task hook failed for %s: %s", task_id, exc)

    async def _apply_avatar_generation(
        self,
        session: AsyncSession,
        task: Task,
        target_agent_id: str,
    ) -> None:
        """Find generated image in operator workspace and apply as avatar.

        Uses the avatar service layer (save_avatar_bytes) instead of
        writing directly to the database or filesystem.
        """
        from pathlib import Path

        from hermeshq.config import get_settings
        from hermeshq.services.avatar import save_avatar_bytes, AVATAR_MEDIA_TYPES, delete_avatar_files

        settings = get_settings()

        # Operator workspace: work/ directory where hermes-agent saves files
        operator_workspace = Path(settings.workspaces_root) / f"agent-{task.agent_id}" / "work"
        if not operator_workspace.exists():
            return

        # Find image files (png, jpg, jpeg, webp, svg) sorted newest first
        image_extensions = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
        candidates = sorted(
            [f for f in operator_workspace.rglob("*") if f.is_file() and f.suffix.lower() in image_extensions],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return

        # Take the most recent image
        source = candidates[0]

        # Resolve content type from extension
        ext = source.suffix.lower()
        if ext == ".jpeg":
            ext = ".jpg"
        content_type = AVATAR_MEDIA_TYPES.get(ext, "image/png")

        # Use the avatar service layer to save
        avatar_base = Path(settings.agent_assets_root) if settings.agent_assets_root else Path(settings.workspaces_root) / "_agent_assets"
        content = source.read_bytes()
        filename = save_avatar_bytes(avatar_base, target_agent_id, content, content_type)

        # Update target agent in DB
        target_agent = await session.get(Agent, target_agent_id)
        if target_agent:
            target_agent.avatar_filename = filename
            try:
                await session.commit()
            except Exception:
                delete_avatar_files(avatar_base, target_agent_id)
                raise


            await self._log(
                session,
                "agent.avatar.generated",
                agent=target_agent,
                task=task,
                message="AI avatar applied from operator task",
            )
            await session.commit()
            await self.event_broker.publish(
                {
                    "type": "agent.avatar_updated",
                    "agent_id": target_agent_id,
                }
            )

# ---------------------------------------------------------------------------
# Module-level helper to get the running supervisor from the FastAPI app.
# ---------------------------------------------------------------------------

def get_supervisor() -> AgentSupervisor:
    """Return the AgentSupervisor attached to the running FastAPI app state."""
    # Lazy import to avoid circular dependency at module level.
    from hermeshq.main import app  # noqa: WPS433

    supervisor: AgentSupervisor | None = getattr(app.state, "supervisor", None)
    if supervisor is None:
        raise RuntimeError("AgentSupervisor not initialised yet")
    return supervisor
