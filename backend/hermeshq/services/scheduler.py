import asyncio
import contextlib
import logging
from datetime import datetime, timezone

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.activity import ActivityLog
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task

logger = logging.getLogger(__name__)


class SchedulerService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        on_task_created,
    ) -> None:
        self.session_factory = session_factory
        self.on_task_created = on_task_created
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(5)

    async def tick(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            result = await session.execute(
                select(ScheduledTask).where(ScheduledTask.enabled == True)  # noqa: E712
            )
            schedules = result.scalars().all()
            created_task_ids: list[str] = []
            for schedule in schedules:
                schedule.next_run = self._ensure_utc(schedule.next_run)
                schedule.last_run = self._ensure_utc(schedule.last_run)
                next_run = schedule.next_run or self._get_next_run(schedule.cron_expression, now)
                if not schedule.next_run:
                    schedule.next_run = next_run
                if schedule.next_run and schedule.next_run <= now:
                    task = Task(
                        agent_id=schedule.agent_id,
                        title=schedule.name,
                        prompt=schedule.prompt,
                        metadata_json={"scheduled_task_id": schedule.id, "scheduled": True},
                    )
                    session.add(task)
                    await session.flush()
                    created_task_ids.append(task.id)
                    schedule.last_run = now
                    schedule.next_run = self._get_next_run(schedule.cron_expression, now)
                    session.add(
                        ActivityLog(
                            agent_id=schedule.agent_id,
                            task_id=task.id,
                            event_type="schedule.triggered",
                            message=schedule.name,
                            details={"schedule_id": schedule.id},
                        )
                    )
            await session.commit()
        for task_id in created_task_ids:
            await self.on_task_created(task_id)

    def _get_next_run(self, expression: str, now: datetime) -> datetime:
        fields = expression.split()
        if len(fields) == 6:
            return self._ensure_utc(croniter(expression, now, second_at_beginning=True).get_next(datetime))
        return self._ensure_utc(croniter(expression, now).get_next(datetime))

    def _ensure_utc(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
