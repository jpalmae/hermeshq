from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import desc, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

logger = logging.getLogger(__name__)


class _ResizableLimiter:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0
        self._condition = asyncio.Condition()

    async def acquire(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._active < self._limit)
            self._active += 1

    async def release(self) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()

    async def resize(self, limit: int) -> None:
        async with self._condition:
            self._limit = limit
            self._condition.notify_all()


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
        claim_token: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._task_id = task_id
        self._agent_id = agent_id
        self._user_id = user_id
        self._claim_token = claim_token
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
        await self._event_broker.publish(
            event,
            audience=EventAudience.for_agent(self._agent_id, user_id=self._user_id),
        )

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

        try:
            async with self._session_factory() as session:
                statement = select(Task).where(Task.id == self._task_id)
                if self._claim_token:
                    statement = statement.where(
                        Task.status == "running",
                        Task.claim_token == self._claim_token,
                    )
                task_row = (await session.execute(statement)).scalar_one_or_none()
                agent_row = await session.get(Agent, task_row.agent_id) if task_row else None
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
        except Exception:
            self._deltas = snapshots + self._deltas
            raise

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
from hermeshq.core.events import EventAudience, EventBroker
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
from hermeshq.services.task_board import sync_board_with_runtime


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
        self.secret_vault = secret_vault
        self.runtimes: dict[str, Any] = {"hermes": runtime}
        self.running_agents: set[str] = set()
        self.active_tasks: dict[str, asyncio.Task] = {}
        self.gateway_supervisor: object | None = None
        settings = get_settings()
        self._concurrency_limiter = _ResizableLimiter(settings.concurrency_semaphore)
        self._semaphore_value = settings.concurrency_semaphore
        self._queue_poll_seconds = settings.task_queue_poll_seconds
        self._lease_seconds = settings.task_lease_seconds
        self._heartbeat_seconds = settings.task_heartbeat_seconds
        self._max_attempts = settings.task_max_attempts
        self._worker_id = str(uuid4())
        self._dispatcher_task: asyncio.Task | None = None
        self._dispatch_wakeup = asyncio.Event()
        self._active_claims: dict[str, str] = {}
        self._requeue_on_cancel: set[str] = set()
        self._stopping = False

    def register_runtime(self, runtime_type: str, runtime: object) -> None:
        """Register an additional runtime (e.g. 'pi' for PiRuntime)."""
        self.runtimes[runtime_type] = runtime

    async def update_semaphore(self, new_value: int) -> None:
        self._semaphore_value = new_value
        await self._concurrency_limiter.resize(new_value)
        self._dispatch_wakeup.set()

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
            select(Task).where(Task.agent_id == task.agent_id).order_by(desc(Task.queued_at)).limit(24)
        )
        candidates = list(result.scalars().all())
        prior_turns = [
            item
            for item in reversed(candidates)
            if item.id != task.id
            and item.status == "completed"
            and (item.metadata_json or {}).get("conversation")
            and (not thread_id or str((item.metadata_json or {}).get("thread_id") or "").strip() == thread_id)
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

        await self._recover_expired_tasks()
        self._stopping = False
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(self._dispatch_loop())
        self._dispatch_wakeup.set()

    async def shutdown_runtime(self) -> None:
        self._stopping = True
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher_task
            self._dispatcher_task = None
        runners = list(self.active_tasks.items())
        self._requeue_on_cancel.update(task_id for task_id, _ in runners)
        for _, runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*(runner for _, runner in runners), return_exceptions=True)
        try:
            await self._requeue_worker_claims()
        except Exception:
            logger.exception("Failed to release task claims during shutdown")

    async def _requeue_worker_claims(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Task)
                .where(Task.status == "running", Task.claimed_by == self._worker_id)
                .with_for_update(skip_locked=True)
            )
            tasks = list(result.scalars().all())
            for task in tasks:
                cancelled = task.cancel_requested_at is not None
                task.status = "cancelled" if cancelled else "queued"
                task.started_at = task.started_at if cancelled else None
                task.completed_at = utcnow() if cancelled else None
                task.claimed_by = None
                task.claim_token = None
                task.claimed_at = None
                task.lease_expires_at = None
                if not cancelled:
                    task.cancel_requested_at = None
                    task.attempt_count = max(0, task.attempt_count - 1)
                await sync_board_with_runtime(session, task.id, task.status)
                session.add(
                    ActivityLog(
                        agent_id=task.agent_id,
                        task_id=task.id,
                        event_type="task.cancelled" if cancelled else "task.requeued",
                        message="Task cancelled during shutdown"
                        if cancelled
                        else "Task returned to queue during shutdown",
                        details={"attempt_count": task.attempt_count},
                    )
                )
            if tasks:
                await session.commit()

    async def _recover_expired_tasks(self) -> int:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Task)
                .where(
                    Task.status == "running",
                    or_(Task.lease_expires_at.is_(None), Task.lease_expires_at <= utcnow()),
                )
                .with_for_update(skip_locked=True)
            )
            expired = result.scalars().all()
            if not expired:
                return 0

            now = utcnow()
            for task in expired:
                if task.cancel_requested_at is not None:
                    task.status = "cancelled"
                    task.completed_at = now
                    event_type = "task.cancelled"
                    message = "Expired execution cancelled"
                elif task.attempt_count >= self._max_attempts:
                    task.status = "failed"
                    task.completed_at = now
                    task.error_message = f"Task lease expired after {task.attempt_count} execution attempts."
                    event_type = "task.failed"
                    message = "Task exceeded recovery attempts"
                else:
                    task.status = "queued"
                    task.started_at = None
                    task.completed_at = None
                    task.error_message = None
                    event_type = "task.requeued"
                    message = "Expired execution returned to queue"
                task.claimed_by = None
                task.claim_token = None
                task.claimed_at = None
                task.lease_expires_at = None
                if task.status != "cancelled":
                    task.cancel_requested_at = None
                await sync_board_with_runtime(session, task.id, task.status)
                session.add(
                    ActivityLog(
                        agent_id=task.agent_id,
                        task_id=task.id,
                        event_type=event_type,
                        message=message,
                        details={"attempt_count": task.attempt_count},
                    )
                )

            await session.commit()
            logger.warning("Recovered %d tasks with expired execution leases", len(expired))
            return len(expired)

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
            },
            audience=EventAudience.for_agent(agent_id),
        )
        await self._start_pending_tasks(agent_id)
        await self._ensure_gateways_alive(agent_id)
        return agent

    async def _ensure_gateways_alive(self, agent_id: str) -> None:
        gw = getattr(self, "gateway_supervisor", None)
        if gw is None:
            return
        from hermeshq.models.messaging_channel import MessagingChannel

        async with self.session_factory() as session:
            result = await session.execute(
                select(MessagingChannel).where(
                    MessagingChannel.agent_id == agent_id,
                    MessagingChannel.enabled.is_(True),
                )
            )
            channels = result.scalars().all()

        for channel in channels:
            if channel.platform in ("google_chat", "kapso_whatsapp"):
                continue
            handle = gw.processes.get(agent_id)
            is_alive = handle and handle.process.poll() is None and channel.platform in handle.platforms
            if not is_alive:
                logger.info("Reviving dead gateway for %s/%s on agent start", agent_id, channel.platform)
                try:
                    await gw.start_channel(agent_id, channel.platform)
                except Exception:
                    logger.warning("Failed to revive gateway %s/%s", agent_id, channel.platform, exc_info=True)

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
            },
            audience=EventAudience.for_agent(agent_id),
        )
        return agent

    async def restart_agent(self, agent_id: str) -> Agent:
        await self.stop_agent(agent_id)
        return await self.start_agent(agent_id)

    async def submit_task(self, task_id: str) -> None:
        self._dispatch_wakeup.set()

    async def _run_claimed_task(self, task_id: str, claim_token: str) -> None:
        execution_task: asyncio.Task | None = None
        heartbeat_task: asyncio.Task | None = None
        try:
            execution_task = asyncio.create_task(self._run_task(task_id, claim_token))
            heartbeat_task = asyncio.create_task(self._heartbeat_claim(task_id, claim_token, execution_task))
            await execution_task
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if execution_task is not None and not execution_task.done():
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
            await self._concurrency_limiter.release()
            self.active_tasks.pop(task_id, None)
            self._active_claims.pop(task_id, None)
            self._requeue_on_cancel.discard(task_id)
            self._dispatch_wakeup.set()

    async def cancel_task(self, task_id: str) -> None:
        publish_event: dict | None = None
        async with self.session_factory() as session:
            task = (
                await session.execute(select(Task).where(Task.id == task_id).with_for_update())
            ).scalar_one_or_none()
            if task is None or task.status not in {"pending", "queued", "running"}:
                return
            if task.status in {"pending", "queued"}:
                task.status = "cancelled"
                task.completed_at = utcnow()
                task.cancel_requested_at = utcnow()
                await sync_board_with_runtime(session, task.id, task.status)
                session.add(
                    ActivityLog(
                        agent_id=task.agent_id,
                        task_id=task.id,
                        event_type="task.cancelled",
                        message=task.title or "Task cancelled",
                    )
                )
                publish_event = {
                    "type": "task.cancelled",
                    "task_id": task.id,
                    "agent_id": task.agent_id,
                }
                if task.created_by_user_id:
                    publish_event["created_by_user_id"] = task.created_by_user_id
            else:
                task.cancel_requested_at = utcnow()
            await session.commit()

        runner = self.active_tasks.get(task_id)
        if runner:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        else:
            self._dispatch_wakeup.set()
        if publish_event is not None:
            await self.event_broker.publish(
                publish_event,
                audience=EventAudience.for_agent(
                    publish_event["agent_id"],
                    user_id=publish_event.get("created_by_user_id"),
                ),
            )

    async def _start_pending_tasks(self, agent_id: str) -> None:
        self._dispatch_wakeup.set()

    async def _dispatch_loop(self) -> None:
        try:
            while not self._stopping:
                self._dispatch_wakeup.clear()
                try:
                    await self._recover_expired_tasks()
                    await self._cancel_requested_claims()
                    await self._dispatch_available_tasks()
                except Exception:
                    logger.exception("Durable task dispatcher tick failed")
                try:
                    await asyncio.wait_for(self._dispatch_wakeup.wait(), timeout=self._queue_poll_seconds)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    async def _dispatch_available_tasks(self) -> None:
        while not self._stopping and len(self.active_tasks) < self._semaphore_value:
            await self._concurrency_limiter.acquire()
            try:
                claim = await self._claim_next_task()
            except BaseException:
                await self._concurrency_limiter.release()
                raise
            if claim is None:
                await self._concurrency_limiter.release()
                return
            task_id, claim_token = claim
            runner = asyncio.create_task(self._run_claimed_task(task_id, claim_token))
            self.active_tasks[task_id] = runner
            self._active_claims[task_id] = claim_token

    async def _claim_next_task(self) -> tuple[str, str] | None:
        queued_task = aliased(Task)
        running_task = aliased(Task)
        head_queued_at = (
            select(queued_task.queued_at)
            .where(queued_task.agent_id == Agent.id, queued_task.status == "queued")
            .order_by(queued_task.queued_at.asc(), queued_task.id.asc())
            .limit(1)
            .scalar_subquery()
        )
        has_running_task = exists(
            select(running_task.id).where(
                running_task.agent_id == Agent.id,
                running_task.status == "running",
            )
        )
        async with self.session_factory() as session:
            agent = (
                await session.execute(
                    select(Agent)
                    .where(
                        Agent.status == "running",
                        Agent.is_archived.is_(False),
                        head_queued_at.is_not(None),
                        ~has_running_task,
                    )
                    .order_by(head_queued_at.asc(), Agent.id.asc())
                    .with_for_update(of=Agent, skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if agent is None:
                return None

            task = (
                await session.execute(
                    select(Task)
                    .where(Task.agent_id == agent.id, Task.status == "queued")
                    .order_by(Task.queued_at.asc(), Task.id.asc())
                    .with_for_update(of=Task)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if task is None:
                return None

            now = utcnow()
            claim_token = str(uuid4())
            task.status = "running"
            task.claimed_by = self._worker_id
            task.claim_token = claim_token
            task.claimed_at = now
            task.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            task.cancel_requested_at = None
            task.attempt_count += 1
            task.started_at = now
            task.completed_at = None
            task.error_message = None
            task.messages_json = []
            task.tool_calls = []
            await sync_board_with_runtime(session, task.id, task.status)
            await self._log(
                session,
                "task.started",
                agent=agent,
                task=task,
                message=task.title or task.prompt[:72],
                details={"attempt_count": task.attempt_count, "worker_id": self._worker_id},
            )
            await session.commit()
            return task.id, claim_token

    async def _heartbeat_claim(
        self,
        task_id: str,
        claim_token: str,
        execution_task: asyncio.Task,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_seconds)
                try:
                    async with self.session_factory() as session:
                        result = await session.execute(
                            update(Task)
                            .where(
                                Task.id == task_id,
                                Task.status == "running",
                                Task.claim_token == claim_token,
                                Task.cancel_requested_at.is_(None),
                            )
                            .values(lease_expires_at=utcnow() + timedelta(seconds=self._lease_seconds))
                        )
                        await session.commit()
                        if getattr(result, "rowcount", 0) == 1:
                            continue
                        current_status = await session.scalar(select(Task.status).where(Task.id == task_id))
                        if current_status in {"completed", "failed", "cancelled"}:
                            return
                except Exception:
                    logger.exception("Task lease heartbeat failed for %s", task_id)
                    self._requeue_on_cancel.add(task_id)
                if not execution_task.done():
                    execution_task.cancel()
                return
        except asyncio.CancelledError:
            return

    async def _cancel_requested_claims(self) -> None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Task.id).where(
                    Task.status == "running",
                    Task.claimed_by == self._worker_id,
                    Task.cancel_requested_at.is_not(None),
                )
            )
            task_ids = list(result.scalars().all())
        for task_id in task_ids:
            runner = self.active_tasks.get(task_id)
            if runner is not None:
                runner.cancel()

    async def _run_task(self, task_id: str, claim_token: str) -> None:
        callbacks: list = []
        try:
            conversation_history: list[dict] = []
            session_id: str | None = None
            async with self.session_factory() as session:
                task = (
                    await session.execute(
                        select(Task).where(
                            Task.id == task_id,
                            Task.status == "running",
                            Task.claim_token == claim_token,
                        )
                    )
                ).scalar_one_or_none()
                if task is None:
                    return
                if task.cancel_requested_at is not None:
                    raise asyncio.CancelledError
                agent = await session.get(Agent, task.agent_id)
                if not agent:
                    return
                if agent.status != "running":
                    task.status = "cancelled" if agent.is_archived else "queued"
                    task.started_at = None
                    task.completed_at = utcnow() if agent.is_archived else None
                    task.claimed_by = None
                    task.claim_token = None
                    task.claimed_at = None
                    task.lease_expires_at = None
                    task.cancel_requested_at = None
                    if task.status == "queued":
                        task.attempt_count = max(0, task.attempt_count - 1)
                    await sync_board_with_runtime(session, task.id, task.status)
                    await session.commit()
                    self._dispatch_wakeup.set()
                    return
                task_owner_id = task.created_by_user_id
                conversation_history = await self._build_conversation_history(session, task)
                metadata = task.metadata_json or {}
                if metadata.get("conversation"):
                    candidate_session_id = str(metadata.get("thread_id") or "").strip()
                    if candidate_session_id:
                        session_id = candidate_session_id

            event = {
                "type": "task.started",
                "task_id": task_id,
                "agent_id": task.agent_id,
            }
            if task_owner_id:
                event["created_by_user_id"] = task_owner_id
            await self.event_broker.publish(
                event,
                audience=EventAudience.for_agent(task.agent_id, user_id=task_owner_id),
            )

            stream_buffer = _StreamBuffer(
                session_factory=self.session_factory,
                task_id=task_id,
                agent_id=task.agent_id,
                log_func=self._log,
                event_broker=self.event_broker,
                user_id=task_owner_id,
                claim_token=claim_token,
            )
            stream_buffer.start_flush_loop()

            async def stream_callback(delta: str, index: int | None = None) -> None:
                await stream_buffer.push(delta, index)

            try:
                runtime = self.runtimes.get(agent.runtime_type or "hermes", self.runtimes["hermes"])
                execution = await runtime.execute(
                    agent,
                    task,
                    stream_callback,
                    conversation_history=conversation_history,
                    session_id=session_id,
                )
            finally:
                await stream_buffer.stop_flush_loop()
            async with self.session_factory() as session:
                task = (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.id == task_id,
                            Task.status == "running",
                            Task.claim_token == claim_token,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
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
                task.claimed_by = None
                task.claim_token = None
                task.claimed_at = None
                task.lease_expires_at = None
                task.cancel_requested_at = None

                response_attachments = getattr(execution, "response_attachments", [])
                if response_attachments:
                    metadata = dict(task.metadata_json or {})
                    metadata["response_attachments"] = [
                        {k: v for k, v in att.items() if k != "source_path"} for att in response_attachments
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
                    callbacks=callbacks,
                )
                await self._queue_external_callback_delivery(
                    session,
                    task=task,
                    agent=agent,
                    success=True,
                    summary=execution.final_response,
                    callbacks=callbacks,
                )
                await session.commit()
                await self._drain_callbacks(callbacks)

            completed_event = {
                "type": "task.completed",
                "task_id": task_id,
                "agent_id": task.agent_id,
                "response": execution.final_response,
                "metadata": task.metadata_json or {},
            }
            if task_owner_id:
                completed_event["created_by_user_id"] = task_owner_id
            await self.event_broker.publish(
                completed_event,
                audience=EventAudience.for_agent(task.agent_id, user_id=task_owner_id),
            )

            await self._run_post_task_hooks(task_id)

        except asyncio.CancelledError:
            publish_event: dict | None = None
            async with self.session_factory() as session:
                task = (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.id == task_id,
                            Task.status == "running",
                            Task.claim_token == claim_token,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                agent = await session.get(Agent, task.agent_id) if task else None
                if task:
                    requeue = task_id in self._requeue_on_cancel and task.cancel_requested_at is None
                    task.status = "queued" if requeue else "cancelled"
                    await sync_board_with_runtime(session, task.id, task.status)
                    task.started_at = None if requeue else task.started_at
                    task.completed_at = None if requeue else utcnow()
                    task.claimed_by = None
                    task.claim_token = None
                    task.claimed_at = None
                    task.lease_expires_at = None
                    if requeue:
                        task.cancel_requested_at = None
                        task.attempt_count = max(0, task.attempt_count - 1)
                if task and agent:
                    agent.last_activity = utcnow()
                    await self._log(
                        session,
                        "task.requeued" if task.status == "queued" else "task.cancelled",
                        agent=agent,
                        task=task,
                        message=task.title or ("Task requeued" if task.status == "queued" else "Task cancelled"),
                    )
                await session.commit()
                if task and task.status == "cancelled":
                    publish_event = {
                        "type": "task.cancelled",
                        "task_id": task_id,
                        "agent_id": task.agent_id,
                    }
                    if task.created_by_user_id:
                        publish_event["created_by_user_id"] = task.created_by_user_id
            if publish_event is not None:
                await self.event_broker.publish(
                    publish_event,
                    audience=EventAudience.for_agent(
                        publish_event["agent_id"],
                        user_id=publish_event.get("created_by_user_id"),
                    ),
                )
        except Exception as exc:  # noqa: BLE001  # task cancellation catch-all
            failed_event: dict | None = None
            async with self.session_factory() as session:
                task = (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.id == task_id,
                            Task.status == "running",
                            Task.claim_token == claim_token,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                agent = await session.get(Agent, task.agent_id) if task else None
                if task:
                    task.status = "failed"
                    await sync_board_with_runtime(session, task.id, task.status)
                    task.completed_at = utcnow()
                    task.error_message = str(exc)
                    task.claimed_by = None
                    task.claim_token = None
                    task.claimed_at = None
                    task.lease_expires_at = None
                    task.cancel_requested_at = None
                if task and agent:
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
                        callbacks=callbacks,
                    )
                    await self._queue_external_callback_delivery(
                        session,
                        task=task,
                        agent=agent,
                        success=False,
                        summary=str(exc),
                        callbacks=callbacks,
                    )
                await session.commit()
                await self._drain_callbacks(callbacks)
                if task:
                    failed_event = {
                        "type": "task.failed",
                        "task_id": task_id,
                        "agent_id": task.agent_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                    if task.created_by_user_id:
                        failed_event["created_by_user_id"] = task.created_by_user_id
            if failed_event is not None:
                await self.event_broker.publish(
                    failed_event,
                    audience=EventAudience.for_agent(
                        failed_event["agent_id"],
                        user_id=failed_event.get("created_by_user_id"),
                    ),
                )

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
        callbacks: list,
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
                },
                audience=EventAudience.for_agents(agent.id, source_agent.id),
            )
            pty_manager = getattr(self, "pty_manager", None)
            if pty_manager is not None:
                notice = f"\r\n[HermesHQ] Delegation result from {child_name}: {status_label}. Task {task.id}\r\n"
                await pty_manager.broadcast_notice(source_agent.id, notice)

        callbacks.append(_after_commit)

    async def _queue_external_callback_delivery(
        self,
        session: AsyncSession,
        *,
        task: Task,
        agent: Agent,
        success: bool,
        summary: str,
        callbacks: list,
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
                from telegram import Bot

                bot = Bot(token=token)
                await bot.send_message(chat_id=chat_id, text=message_text, message_thread_id=thread_value)
                await bot.shutdown()
            except Exception:  # noqa: BLE001  # telegram errors must never affect task state
                logger.warning("Failed to send Telegram notification to chat %s", chat_id, exc_info=True)

        callbacks.append(_after_commit)

    async def _drain_callbacks(self, callbacks: list) -> None:
        """Run post-commit callbacks one by one; a failing callback never
        aborts the rest nor propagates into task state handling."""
        for callback in callbacks:
            try:
                await callback()
            except Exception:  # noqa: BLE001  # post-commit side effect — log and continue
                logger.exception("Post-commit task callback failed")

    async def get_recent_activity(self, limit: int = 20) -> list[ActivityLog]:
        async with self.session_factory() as session:
            result = await session.execute(select(ActivityLog).order_by(desc(ActivityLog.created_at)).limit(limit))
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
        from hermeshq.services.avatar import AVATAR_MEDIA_TYPES, delete_avatar_files, save_avatar_bytes

        settings = get_settings()

        # Operator workspace: work/ directory where hermes-agent saves files
        operator_workspace = Path(settings.workspaces_root) / f"agent-{task.agent_id}" / "work"
        if not operator_workspace.exists():
            return

        # Find image files (png, jpg, jpeg, webp, svg) sorted newest first
        image_extensions = {".png", ".jpg", ".jpeg", ".webp"}
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
        avatar_base = (
            Path(settings.agent_assets_root)
            if settings.agent_assets_root
            else Path(settings.workspaces_root) / "_agent_assets"
        )
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
                },
                audience=EventAudience.for_agent(target_agent_id),
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
