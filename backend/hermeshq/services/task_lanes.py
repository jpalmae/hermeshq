from __future__ import annotations

from typing import Any

from sqlalchemy import case, func

from hermeshq.services import admision


def task_class(task: Any) -> str:
    metadata = getattr(task, "metadata_json", None) or {}
    if metadata.get("scheduled"):
        return admision.CRON
    if metadata.get("background_job"):
        return admision.SEGUNDO_PLANO
    return admision.CHAT


def _flag(model, key: str):
    return func.lower(func.coalesce(model.metadata_json[key].as_string(), "")).in_(("true", "1"))


def task_class_expression(model):
    return case(
        (_flag(model, "scheduled"), admision.CRON),
        (_flag(model, "background_job"), admision.SEGUNDO_PLANO),
        else_=admision.CHAT,
    )
