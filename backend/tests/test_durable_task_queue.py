from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import requires_database
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.base import utcnow
from hermeshq.models.node import Node
from hermeshq.models.task import Task
from hermeshq.services.agent_supervisor import AgentSupervisor

pytestmark = [pytest.mark.integration, requires_database]


class BlockingRuntime:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.releases: dict[str, asyncio.Event] = {}

    async def execute(self, agent, task, stream_callback, **kwargs):
        self.started.append(task.id)
        release = self.releases.setdefault(task.id, asyncio.Event())
        await release.wait()
        return SimpleNamespace(
            final_response=f"completed:{task.id}",
            tokens_used=1,
            iterations=1,
            messages=[],
            tool_calls=[],
            engine="test",
            response_attachments=[],
        )


def make_supervisor(session_factory, runtime=None) -> AgentSupervisor:
    broker = MagicMock()
    broker.publish = AsyncMock()
    return AgentSupervisor(
        session_factory=session_factory,
        event_broker=broker,
        runtime=runtime or MagicMock(),
        secret_vault=MagicMock(),
    )


async def wait_until(predicate, timeout: float = 5) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.02)


async def queue_fixture(db_session: AsyncSession) -> tuple[Agent, Agent, list[Task]]:
    node = Node(name="queue-node", hostname="queue-node")
    db_session.add(node)
    await db_session.flush()
    first_agent = Agent(
        node_id=node.id,
        name="Queue Agent A",
        slug="queue-agent-a",
        workspace_path="/tmp/queue-agent-a",
        status="running",
    )
    second_agent = Agent(
        node_id=node.id,
        name="Queue Agent B",
        slug="queue-agent-b",
        workspace_path="/tmp/queue-agent-b",
        status="running",
    )
    db_session.add_all([first_agent, second_agent])
    await db_session.flush()
    now = utcnow()
    tasks = [
        Task(agent_id=first_agent.id, prompt="a1", queued_at=now),
        Task(agent_id=first_agent.id, prompt="a2", queued_at=now + timedelta(milliseconds=1)),
        Task(agent_id=second_agent.id, prompt="b1", queued_at=now + timedelta(milliseconds=2)),
    ]
    db_session.add_all(tasks)
    await db_session.commit()
    return first_agent, second_agent, tasks


async def test_dispatcher_runs_one_task_per_agent_and_preserves_fifo(db_engine, db_session) -> None:
    _, _, tasks = await queue_fixture(db_session)
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    runtime = BlockingRuntime()
    supervisor = make_supervisor(session_factory, runtime)

    await supervisor.bootstrap_runtime()
    try:
        await wait_until(lambda: len(runtime.started) == 2)
        assert tasks[0].id in runtime.started
        assert tasks[2].id in runtime.started
        assert tasks[1].id not in runtime.started

        runtime.releases[tasks[0].id].set()
        await wait_until(lambda: tasks[1].id in runtime.started)
        runtime.releases[tasks[1].id].set()
        runtime.releases[tasks[2].id].set()
        await wait_until(lambda: not supervisor.active_tasks)

        async with session_factory() as session:
            persisted = list(
                (await session.execute(select(Task).where(Task.id.in_([task.id for task in tasks])))).scalars()
            )
        assert {task.status for task in persisted} == {"completed"}
        assert {task.attempt_count for task in persisted} == {1}
    finally:
        await supervisor.shutdown_runtime()


async def test_expired_claim_is_requeued_without_losing_queued_work(db_engine, db_session) -> None:
    first_agent, _, tasks = await queue_fixture(db_session)
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    supervisor = make_supervisor(session_factory)

    tasks[0].status = "running"
    tasks[0].attempt_count = 1
    tasks[0].claimed_by = "dead-worker"
    tasks[0].claim_token = "dead-claim"
    tasks[0].claimed_at = utcnow() - timedelta(minutes=2)
    tasks[0].lease_expires_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()

    recovered = await supervisor._recover_expired_tasks()

    assert recovered == 1
    async with session_factory() as session:
        first = await session.get(Task, tasks[0].id)
        second = await session.get(Task, tasks[1].id)
    assert first is not None and first.status == "queued"
    assert first.claim_token is None
    assert first.attempt_count == 1
    assert second is not None and second.status == "queued"
    assert second.agent_id == first_agent.id


async def test_concurrent_dispatchers_cannot_claim_two_tasks_for_one_agent(db_engine, db_session) -> None:
    _, second_agent, tasks = await queue_fixture(db_session)
    second_agent.status = "stopped"
    await db_session.commit()
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    supervisors = [make_supervisor(session_factory), make_supervisor(session_factory)]

    claims = await asyncio.gather(*(supervisor._claim_next_task() for supervisor in supervisors))

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert claimed[0][0] == tasks[0].id
    async with session_factory() as session:
        first = await session.get(Task, tasks[0].id)
        second = await session.get(Task, tasks[1].id)
    assert first is not None and first.status == "running"
    assert second is not None and second.status == "queued"


async def test_graceful_shutdown_returns_active_task_to_queue(db_engine, db_session) -> None:
    _, second_agent, tasks = await queue_fixture(db_session)
    second_agent.status = "stopped"
    await db_session.commit()
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    runtime = BlockingRuntime()
    supervisor = make_supervisor(session_factory, runtime)

    await supervisor.bootstrap_runtime()
    await wait_until(lambda: runtime.started == [tasks[0].id])
    await supervisor.shutdown_runtime()

    async with session_factory() as session:
        first = await session.get(Task, tasks[0].id)
        second = await session.get(Task, tasks[1].id)
    assert first is not None and first.status == "queued"
    assert first.claim_token is None
    assert first.attempt_count == 0
    assert second is not None and second.status == "queued"


async def test_queued_cancellation_is_persisted(db_engine, db_session) -> None:
    _, _, tasks = await queue_fixture(db_session)
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    supervisor = make_supervisor(session_factory)

    await supervisor.cancel_task(tasks[0].id)

    async with session_factory() as session:
        task = await session.get(Task, tasks[0].id)
    assert task is not None
    assert task.status == "cancelled"
    assert task.completed_at is not None
    assert task.cancel_requested_at is not None


async def test_running_cancellation_releases_next_task(db_engine, db_session) -> None:
    _, second_agent, tasks = await queue_fixture(db_session)
    second_agent.status = "stopped"
    await db_session.commit()
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    runtime = BlockingRuntime()
    supervisor = make_supervisor(session_factory, runtime)

    await supervisor.bootstrap_runtime()
    try:
        await wait_until(lambda: runtime.started == [tasks[0].id])
        await supervisor.cancel_task(tasks[0].id)
        await wait_until(lambda: tasks[1].id in runtime.started)

        async with session_factory() as session:
            cancelled = await session.get(Task, tasks[0].id)
        assert cancelled is not None and cancelled.status == "cancelled"
        runtime.releases[tasks[1].id].set()
        await wait_until(lambda: not supervisor.active_tasks)
    finally:
        await supervisor.shutdown_runtime()
