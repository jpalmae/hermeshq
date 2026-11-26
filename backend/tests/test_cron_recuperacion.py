from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from conftest import requires_database, run_queued
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_execution_lease import _seed_task

from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.services.agent_supervisor import AgentSupervisor
from hermeshq.services.cron_schedule import compute_next_run
from hermeshq.services.scheduler import SchedulerService

SANTIAGO = ZoneInfo("America/Santiago")


def _fires(expr: str, start: datetime, count: int) -> list[datetime]:
    out, now = [], start
    for _ in range(count):
        now = compute_next_run(expr, "America/Santiago", now)
        out.append(now)
    return out


class TestTimezoneTransitions:
    def test_daily_job_runs_once_on_fall_back_day(self):
        fires = _fires("30 23 * * *", datetime(2026, 4, 3, 12, tzinfo=UTC), 5)
        local_days = [f.astimezone(SANTIAGO).date() for f in fires]
        assert len(local_days) == len(set(local_days))
        assert all(b > a for a, b in zip(fires, fires[1:], strict=False))
        assert [f.astimezone(SANTIAGO).strftime("%H:%M") for f in fires] == ["23:30"] * 5

    def test_hourly_job_does_not_repeat_the_same_wall_time(self):
        fires = _fires("0 * * * *", datetime(2026, 4, 4, 23, tzinfo=UTC), 8)
        walls = [f.astimezone(SANTIAGO).replace(tzinfo=None) for f in fires]
        assert len(walls) == len(set(walls))

    def test_daily_midnight_job_still_runs_on_spring_forward_day(self):
        fires = _fires("0 0 * * *", datetime(2026, 9, 4, 12, tzinfo=UTC), 4)
        local_days = [f.astimezone(SANTIAGO).date() for f in fires]
        assert local_days == [datetime(2026, 9, d).date() for d in (5, 6, 7, 8)]

    def test_utc_schedules_are_unchanged(self):
        start = datetime(2026, 4, 4, 22, tzinfo=UTC)
        assert compute_next_run("30 23 * * *", "UTC", start) == datetime(2026, 4, 4, 23, 30, tzinfo=UTC)


def _maker(db_engine):
    engine, _ = db_engine
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _schedule(maker, agent_id: str, next_run: datetime, **kw) -> str:
    async with maker() as session:
        item = ScheduledTask(
            agent_id=agent_id,
            name=kw.pop("name", "informe diario"),
            cron_expression=kw.pop("cron_expression", "*/5 * * * *"),
            prompt="Prepara el informe",
            enabled=kw.pop("enabled", True),
            approval_status=kw.pop("approval_status", "approved"),
            timezone="America/Santiago",
            next_run=next_run,
            **kw,
        )
        session.add(item)
        await session.commit()
        return item.id


async def _scheduled_tasks(maker, schedule_id: str) -> list[Task]:
    async with maker() as session:
        tasks = (await session.execute(select(Task))).scalars().all()
    return [t for t in tasks if (t.metadata_json or {}).get("scheduled_task_id") == schedule_id]


@pytest.mark.integration
@requires_database
class TestSchedulerFiring:
    async def test_two_schedulers_fire_a_due_schedule_once(self, db_engine):
        maker = _maker(db_engine)
        agent_id, _ = await _seed_task(maker)
        schedule_id = await _schedule(maker, agent_id, datetime.now(UTC) - timedelta(seconds=2))
        created: list[str] = []

        async def on_created(task_id):
            created.append(task_id)

        schedulers = [SchedulerService(maker, on_created) for _ in range(4)]
        await asyncio.gather(*(s.tick() for s in schedulers))
        await asyncio.gather(*(s.tick() for s in schedulers))
        tasks = await _scheduled_tasks(maker, schedule_id)
        assert len(tasks) == 1
        assert created == [tasks[0].id]
        key = tasks[0].metadata_json["scheduled_fire_key"]
        assert key.startswith(f"{schedule_id}@")
        async with maker() as session:
            schedule = await session.get(ScheduledTask, schedule_id)
            assert schedule.next_run > datetime.now(UTC)

    async def test_same_fire_key_is_rejected_by_the_database(self, db_engine):
        maker = _maker(db_engine)
        agent_id, _ = await _seed_task(maker)
        fire_at = datetime.now(UTC) - timedelta(seconds=2)
        schedule_id = await _schedule(maker, agent_id, fire_at)
        other_id = await _schedule(maker, agent_id, fire_at, name="otro")
        async with maker() as session:
            session.add(
                Task(
                    agent_id=agent_id,
                    prompt="ya disparada",
                    status="completed",
                    metadata_json={
                        "scheduled_task_id": schedule_id,
                        "scheduled": True,
                        "scheduled_fire_key": f"{schedule_id}@{fire_at.isoformat()}",
                    },
                )
            )
            await session.commit()
        created: list[str] = []

        async def on_created(task_id):
            created.append(task_id)

        await SchedulerService(maker, on_created).tick()
        assert len(await _scheduled_tasks(maker, schedule_id)) == 1
        assert len(await _scheduled_tasks(maker, other_id)) == 1
        assert len(created) == 1
        async with maker() as session:
            assert (await session.get(ScheduledTask, schedule_id)).next_run > datetime.now(UTC)

    async def test_late_fire_beyond_grace_is_skipped_and_logged(self, db_engine, monkeypatch):
        maker = _maker(db_engine)
        agent_id, _ = await _seed_task(maker)
        monkeypatch.setattr(SchedulerService, "_misfire_grace", staticmethod(lambda: 600))
        stale = await _schedule(maker, agent_id, datetime.now(UTC) - timedelta(hours=2), name="atrasada")
        recent = await _schedule(maker, agent_id, datetime.now(UTC) - timedelta(minutes=5), name="reciente")
        await SchedulerService(maker, AsyncMock()).tick()
        assert await _scheduled_tasks(maker, stale) == []
        assert len(await _scheduled_tasks(maker, recent)) == 1
        async with maker() as session:
            logs = (
                (await session.execute(select(ActivityLog).where(ActivityLog.event_type == "schedule.skipped_misfire")))
                .scalars()
                .all()
            )
            assert [log.details["schedule_id"] for log in logs] == [stale]
            assert logs[0].details["retraso_segundos"] >= 7000
            assert (await session.get(ScheduledTask, stale)).next_run > datetime.now(UTC)

    async def test_long_running_previous_run_prevents_overlap(self, db_engine):
        maker = _maker(db_engine)
        agent_id, _ = await _seed_task(maker)
        schedule_id = await _schedule(maker, agent_id, datetime.now(UTC) - timedelta(seconds=2))
        async with maker() as session:
            session.add(
                Task(
                    agent_id=agent_id,
                    prompt="anterior",
                    status="running",
                    execution_lane=uuid4().hex[:16],
                    metadata_json={"scheduled_task_id": schedule_id, "scheduled": True},
                )
            )
            await session.commit()
        await SchedulerService(maker, AsyncMock()).tick()
        assert len(await _scheduled_tasks(maker, schedule_id)) == 1


@pytest.mark.integration
@requires_database
class TestScheduledRunAuthorization:
    async def _queued_scheduled(self, maker, **schedule_kw):
        agent_id, _ = await _seed_task(maker)
        schedule_id = await _schedule(maker, agent_id, datetime.now(UTC) + timedelta(hours=1), **schedule_kw)
        async with maker() as session:
            agent = await session.get(Agent, agent_id)
            agent.status = "running"
            task = Task(
                agent_id=agent_id,
                prompt="informe",
                status="queued",
                metadata_json={"scheduled_task_id": schedule_id, "scheduled": True},
            )
            session.add(task)
            await session.commit()
            return schedule_id, task.id

    def _supervisor(self, maker):
        runtime = MagicMock()
        runtime.execute = AsyncMock()
        return AgentSupervisor(maker, MagicMock(publish=AsyncMock()), runtime, secret_vault=None), runtime

    @pytest.mark.parametrize("schedule_kw", [{"enabled": False}, {"approval_status": "pending"}])
    async def test_queued_run_of_revoked_schedule_is_not_executed(self, db_engine, schedule_kw):
        maker = _maker(db_engine)
        _, task_id = await self._queued_scheduled(maker, **schedule_kw)
        supervisor, runtime = self._supervisor(maker)
        await run_queued(supervisor, task_id)
        runtime.execute.assert_not_awaited()
        async with maker() as session:
            task = await session.get(Task, task_id)
        assert task.status == "cancelled"
        assert task.metadata_json["end_reason"] == "programacion_no_vigente"

    async def test_deleted_schedule_run_is_not_executed(self, db_engine):
        maker = _maker(db_engine)
        schedule_id, task_id = await self._queued_scheduled(maker)
        async with maker() as session:
            await session.delete(await session.get(ScheduledTask, schedule_id))
            await session.commit()
        supervisor, runtime = self._supervisor(maker)
        await run_queued(supervisor, task_id)
        runtime.execute.assert_not_awaited()
