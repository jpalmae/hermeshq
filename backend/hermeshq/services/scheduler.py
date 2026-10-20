import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.activity import ActivityLog
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.services.cron_schedule import build_scheduled_prompt, compute_next_run, scheduled_task_metadata

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
        self._tick_lock = asyncio.Lock()

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
            except Exception:  # noqa: BLE001  # scheduler tick — must not crash loop
                logger.exception("Scheduler tick failed")
            await asyncio.sleep(5)

    async def tick(self) -> None:
        if self._tick_lock.locked():
            return
        async with self._tick_lock:
            await self._tick_once()

    async def _tick_once(self) -> None:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            statement = select(ScheduledTask).where(
                ScheduledTask.enabled == True,  # noqa: E712
                ScheduledTask.approval_status == "approved",
            )
            # session.bind is always None in SQLAlchemy 2.x AsyncSession.
            # PostgreSQL is the only supported backend, so always use SKIP LOCKED
            # to prevent duplicate task creation when multiple workers are running.
            statement = statement.with_for_update(skip_locked=True)
            result = await session.execute(statement)
            schedules = result.scalars().all()
            created_task_ids: list[str] = []
            for schedule in schedules:
                try:
                    schedule.next_run = self._ensure_utc(schedule.next_run)
                    schedule.last_run = self._ensure_utc(schedule.last_run)
                    next_run = schedule.next_run or self._get_next_run(schedule.cron_expression, now, schedule.timezone)
                    if not schedule.next_run:
                        schedule.next_run = next_run
                    if schedule.next_run and schedule.next_run <= now:
                        if await self._has_active_run(session, schedule.id):
                            schedule.next_run = self._get_next_run(schedule.cron_expression, now, schedule.timezone)
                            session.add(
                                ActivityLog(
                                    agent_id=schedule.agent_id,
                                    event_type="schedule.skipped_overlap",
                                    message=schedule.name,
                                    details={"schedule_id": schedule.id},
                                )
                            )
                            continue
                        task = Task(
                            agent_id=schedule.agent_id,
                            title=schedule.name,
                            prompt=build_scheduled_prompt(schedule, now),
                            created_by_user_id=schedule.owner_user_id,
                            metadata_json=scheduled_task_metadata(schedule),
                        )
                        session.add(task)
                        await session.flush()
                        created_task_ids.append(task.id)
                        schedule.last_run = now
                        schedule.next_run = self._get_next_run(schedule.cron_expression, now, schedule.timezone)
                        session.add(
                            ActivityLog(
                                agent_id=schedule.agent_id,
                                task_id=task.id,
                                event_type="schedule.triggered",
                                message=schedule.name,
                                details={"schedule_id": schedule.id},
                            )
                        )
                except Exception:
                    logger.exception("Scheduler: failed to process schedule %s", schedule.id)
            await session.commit()
        for task_id in created_task_ids:
            await self.on_task_created(task_id)

    async def _has_active_run(self, session: AsyncSession, schedule_id: str) -> bool:
        result = await session.execute(
            select(Task.id)
            .where(
                Task.metadata_json["scheduled_task_id"].as_string() == schedule_id,
                Task.status.in_(("pending", "queued", "running")),
            )
            .limit(1)
        )
        return result.first() is not None

    def _get_next_run(self, expression: str, now: datetime, timezone_name: str | None = "UTC") -> datetime:
        return compute_next_run(expression, timezone_name, now)

    def _ensure_utc(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
