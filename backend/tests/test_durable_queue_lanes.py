from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import requires_database
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.config import get_settings
from hermeshq.models.agent import Agent
from hermeshq.models.base import utcnow
from hermeshq.models.node import Node
from hermeshq.models.task import Task
from hermeshq.models.task_execution import ExternalActionRecord, TaskExecutionAttempt
from hermeshq.services import admision
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.task_lanes import task_class

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


async def agent_fixture(db_session: AsyncSession) -> Agent:
    node = Node(name="lanes-node", hostname="lanes-node")
    db_session.add(node)
    await db_session.flush()
    agent = Agent(
        node_id=node.id,
        name="Lanes Agent",
        slug="lanes-agent",
        workspace_path="/tmp/lanes-agent",
        status="running",
    )
    db_session.add(agent)
    await db_session.flush()
    return agent


def settings_with(**overrides):
    real = get_settings()
    values = {name: getattr(real, name) for name in type(real).model_fields}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_task_class_follows_metadata() -> None:
    assert task_class(SimpleNamespace(metadata_json={})) == admision.CHAT
    assert task_class(SimpleNamespace(metadata_json={"scheduled": True})) == admision.CRON
    assert task_class(SimpleNamespace(metadata_json={"background_job": True})) == admision.SEGUNDO_PLANO
    assert task_class(SimpleNamespace(metadata_json={"scheduled": True, "background_job": True})) == admision.CRON


async def test_parallel_lanes_let_chat_run_beside_a_scheduled_run(db_engine, db_session) -> None:
    agent = await agent_fixture(db_session)
    now = utcnow()
    cron_a = Task(agent_id=agent.id, prompt="cron-a", queued_at=now, metadata_json={"scheduled": True})
    cron_b = Task(
        agent_id=agent.id,
        prompt="cron-b",
        queued_at=now + timedelta(milliseconds=1),
        metadata_json={"scheduled": True},
    )
    chat = Task(agent_id=agent.id, prompt="chat", queued_at=now + timedelta(milliseconds=2))
    db_session.add_all([cron_a, cron_b, chat])
    await db_session.commit()
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    runtime = BlockingRuntime()
    with patch(
        "hermeshq.services.agent_supervisor.get_settings",
        return_value=settings_with(task_parallel_lanes=True),
    ):
        supervisor = make_supervisor(session_factory, runtime)
        await supervisor.bootstrap_runtime()
        try:
            await wait_until(lambda: len(runtime.started) == 2)
            assert set(runtime.started) == {cron_a.id, chat.id}
            runtime.releases[cron_a.id].set()
            await wait_until(lambda: cron_b.id in runtime.started)
            runtime.releases[cron_b.id].set()
            runtime.releases[chat.id].set()
            await wait_until(lambda: not supervisor.active_tasks)
        finally:
            await supervisor.shutdown_runtime()
    async with session_factory() as session:
        lanes = dict(
            (await session.execute(select(Task.id, Task.execution_lane).where(Task.agent_id == agent.id))).all()
        )
    assert lanes[cron_a.id] == admision.CRON
    assert lanes[chat.id] == admision.CHAT


async def test_without_parallel_lanes_the_agent_stays_serialized(db_engine, db_session) -> None:
    agent = await agent_fixture(db_session)
    now = utcnow()
    cron = Task(agent_id=agent.id, prompt="cron", queued_at=now, metadata_json={"scheduled": True})
    chat = Task(agent_id=agent.id, prompt="chat", queued_at=now + timedelta(milliseconds=1))
    db_session.add_all([cron, chat])
    await db_session.commit()
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    supervisor = make_supervisor(session_factory)

    first = await supervisor._claim_next_task()
    second = await supervisor._claim_next_task()

    assert first is not None and first[0] == cron.id
    assert second is None


async def test_reserved_slot_keeps_room_for_chat(db_engine, db_session) -> None:
    agent = await agent_fixture(db_session)
    other = Agent(
        node_id=agent.node_id,
        name="Other",
        slug="lanes-other",
        workspace_path="/tmp/lanes-other",
        status="running",
    )
    db_session.add(other)
    await db_session.flush()
    now = utcnow()
    cron = Task(agent_id=agent.id, prompt="cron", queued_at=now, metadata_json={"scheduled": True})
    background = Task(
        agent_id=other.id,
        prompt="bg",
        queued_at=now + timedelta(milliseconds=1),
        metadata_json={"background_job": True},
    )
    db_session.add_all([cron, background])
    await db_session.commit()
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    with patch(
        "hermeshq.services.agent_supervisor.get_settings",
        return_value=settings_with(concurrency_semaphore=2, concurrency_interactive_reserve=1),
    ):
        supervisor = make_supervisor(session_factory)
    claimed = await supervisor._claim_next_task()
    assert claimed is not None and claimed[0] == cron.id
    assert await supervisor._claim_next_task() is None
    assert supervisor._admitted_classes() == [admision.CHAT]


async def _expired_running(db_session: AsyncSession, agent: Agent, **metadata) -> Task:
    task = Task(
        agent_id=agent.id,
        prompt="trabajo",
        status="running",
        attempt_count=1,
        claimed_by="dead-worker",
        claim_token="dead-claim",
        claimed_at=utcnow() - timedelta(minutes=2),
        lease_expires_at=utcnow() - timedelta(minutes=1),
        metadata_json=metadata,
    )
    db_session.add(task)
    await db_session.flush()
    return task


async def test_expired_claim_after_an_external_action_is_not_requeued(db_engine, db_session) -> None:
    agent = await agent_fixture(db_session)
    task = await _expired_running(db_session, agent, source="public_chat")
    attempt = TaskExecutionAttempt(task_id=task.id, execution_id="exec-1", state="running", lease_generation=1)
    db_session.add(attempt)
    await db_session.flush()
    db_session.add(
        ExternalActionRecord(
            task_id=task.id,
            attempt_id=attempt.attempt_id,
            lease_generation=1,
            kind="correo.enviar",
            fingerprint="f" * 64,
            state="iniciada",
        )
    )
    await db_session.commit()
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    supervisor = make_supervisor(session_factory)

    assert await supervisor._recover_expired_tasks() == 1

    async with session_factory() as session:
        stored = await session.get(Task, task.id)
        live = await session.scalar(
            text("SELECT count(*) FROM task_execution_attempts WHERE task_id = :t AND state IN ('pending', 'running')"),
            {"t": task.id},
        )
    assert stored is not None and stored.status == "failed"
    assert stored.metadata_json["recovery"]["estado"] == "resultado_incierto"
    assert stored.metadata_json["recovery"]["requiere_conciliacion"] is True
    assert stored.claim_token is None
    assert live == 0


async def test_expired_claim_without_effects_is_requeued_for_late_delivery(db_engine, db_session) -> None:
    agent = await agent_fixture(db_session)
    task = await _expired_running(db_session, agent, source="public_chat")
    await db_session.commit()
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    supervisor = make_supervisor(session_factory)

    assert await supervisor._recover_expired_tasks() == 1

    async with session_factory() as session:
        stored = await session.get(Task, task.id)
    assert stored is not None and stored.status == "queued"
    assert stored.metadata_json.get("respuesta_tardia") is True


async def test_stale_scheduled_run_is_not_requeued(db_engine, db_session) -> None:
    agent = await agent_fixture(db_session)
    task = await _expired_running(db_session, agent, scheduled=True, scheduled_task_id="sched-1")
    task.queued_at = utcnow() - timedelta(hours=3)
    await db_session.commit()
    engine, _ = db_engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    supervisor = make_supervisor(session_factory)

    await supervisor._recover_expired_tasks()

    async with session_factory() as session:
        stored = await session.get(Task, task.id)
    assert stored is not None and stored.status == "failed"
    assert stored.metadata_json["recovery"]["estado"] == "programada_vencida"
