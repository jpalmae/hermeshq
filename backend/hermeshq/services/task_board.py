from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update

from hermeshq.models.task import Task

KANBAN_COLUMNS = (
    "inbox",
    "planned",
    "running",
    "blocked",
    "review",
    "done",
    "failed",
)


def next_board_order() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def is_valid_board_column(value: str | None) -> bool:
    return str(value or "").strip() in KANBAN_COLUMNS


def runtime_status_to_board_column(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "running":
        return "running"
    if normalized == "completed":
        return "done"
    if normalized in {"failed", "cancelled"}:
        return "failed"
    return "inbox"


async def sync_board_with_runtime(session, task_id: str, status: str | None) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task_id, Task.board_manual.is_(False))
        .values(
            board_column=runtime_status_to_board_column(status),
            board_order=next_board_order(),
        )
    )
