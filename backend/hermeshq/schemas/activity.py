from datetime import datetime

from hermeshq.schemas.common import ORMModel


class ActivityRead(ORMModel):
    id: str
    agent_id: str | None
    task_id: str | None
    node_id: str | None
    event_type: str
    severity: str
    message: str | None
    details: dict
    created_at: datetime
    updated_at: datetime


class ActivityPageRead(ORMModel):
    items: list[ActivityRead]
    has_more: bool
    next_before_created_at: datetime | None
    next_before_id: str | None
