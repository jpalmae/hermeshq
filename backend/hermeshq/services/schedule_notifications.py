import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.user import User

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 12000
MAX_FAILURE_CHARS = 1000
NOTIFICATION_RETENTION = timedelta(days=7)
NOTIFICATION_DELIVERY_WINDOW = timedelta(hours=12)
BRIDGE_LIVENESS_WINDOW = timedelta(hours=12)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n\n…(resultado recortado, ver HermesHQ)"


async def enqueue_schedule_notification(session: AsyncSession, task, *, success: bool, summary: str | None) -> int:
    metadata = task.metadata_json or {}
    schedule_id = metadata.get("scheduled_task_id")
    if not metadata.get("scheduled") or metadata.get("manual_trigger") or not schedule_id:
        return 0
    try:
        schedule = await session.get(ScheduledTask, schedule_id)
        if not schedule or not schedule.notify_public_chat or not schedule.owner_user_id:
            return 0
        user = await session.get(User, schedule.owner_user_id)
        if not user or not user.is_active:
            return 0
        key_filters = [PublicChatApiKey.bound_user_id == user.id]
        if user.teams_id:
            key_filters.append(
                and_(PublicChatApiKey.trust_sender_id.is_(True), PublicChatApiKey.bound_user_id.is_(None))
            )
        result = await session.execute(
            select(PublicChatApiKey).where(
                PublicChatApiKey.agent_id == schedule.agent_id,
                PublicChatApiKey.is_active.is_(True),
                or_(*key_filters),
            )
        )
        now = datetime.now(UTC)
        keys = [
            key
            for key in result.scalars().all()
            if key.last_notification_poll_at and now - key.last_notification_poll_at <= BRIDGE_LIVENESS_WINDOW
        ]
        if not keys:
            return 0
        if success:
            body = _clip(summary or "", MAX_BODY_CHARS) or "(la tarea terminó sin contenido)"
        else:
            body = f"La tarea programada falló: {_clip(summary or 'error desconocido', MAX_FAILURE_CHARS)}"
        for key in keys:
            session.add(
                PublicChatNotification(
                    api_key_id=key.id,
                    agent_id=schedule.agent_id,
                    recipient_user_id=user.id,
                    sender_id=user.teams_id if key.trust_sender_id else None,
                    schedule_id=schedule.id,
                    task_id=task.id,
                    title=schedule.name[:255],
                    body=body,
                )
            )
        return len(keys)
    except Exception:  # noqa: BLE001  # a notification problem must never fail the task itself
        logger.exception("Failed to enqueue schedule notification for task %s", getattr(task, "id", None))
        return 0


async def purge_old_notifications(session: AsyncSession, api_key_id: str) -> None:
    cutoff = datetime.now(UTC) - NOTIFICATION_RETENTION
    await session.execute(
        delete(PublicChatNotification).where(
            PublicChatNotification.api_key_id == api_key_id,
            PublicChatNotification.created_at < cutoff,
        )
    )
