import asyncio
import traceback

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.core.events import EventBroker
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.node import Node
from hermeshq.models.task import Task
from hermeshq.models.base import utcnow
from hermeshq.services.hermes_runtime import HermesRuntime


class AgentSupervisor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_broker: EventBroker,
        runtime: HermesRuntime,
    ) -> None:
        self.session_factory = session_factory
        self.event_broker = event_broker
        self.runtime = runtime
        self.running_agents: set[str] = set()
        self.active_tasks: dict[str, asyncio.Task] = {}

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
                await session.commit()

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
                await session.commit()
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

    async def get_recent_activity(self, limit: int = 20) -> list[ActivityLog]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ActivityLog).order_by(desc(ActivityLog.created_at)).limit(limit)
            )
            return list(result.scalars().all())
