from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_

from hermeshq.core.events import EventAudience
from hermeshq.models.task import Task


def task_thread_user_id(task: Any) -> str | None:
    metadata = getattr(task, "metadata_json", None) or {}
    value = str(metadata.get("thread_user_id") or "").strip()
    return value or None


def task_audience_user_id(task: Any) -> str | None:
    if getattr(task, "created_by_user_id", None):
        return task.created_by_user_id
    return task_thread_user_id(task)


def task_visible_to_user(task: Any, user_id: str | None) -> bool:
    return bool(user_id) and task_audience_user_id(task) == user_id


def task_visibility_clause(user_id: str):
    return or_(
        Task.created_by_user_id == user_id,
        and_(
            Task.created_by_user_id.is_(None),
            Task.metadata_json["thread_user_id"].as_string() == user_id,
        ),
    )


def task_acts_for_user(task: Any, user_id: str | None) -> bool:
    if not user_id:
        return False
    return task_thread_user_id(task) == user_id or getattr(task, "created_by_user_id", None) == user_id


def task_event_audience(task: Any) -> EventAudience | None:
    user_id = task_audience_user_id(task)
    agent_id = getattr(task, "agent_id", None)
    if not user_id or not agent_id:
        return None
    return EventAudience.for_agent(agent_id, user_id=user_id)
