from datetime import datetime

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel


class ScheduledTaskCreate(BaseModel):
    agent_id: str
    name: str
    cron_expression: str
    prompt: str
    enabled: bool = True


class ScheduledTaskUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    prompt: str | None = None
    enabled: bool | None = None


class ScheduledTaskRead(ORMModel):
    id: str
    agent_id: str
    name: str
    cron_expression: str
    prompt: str
    enabled: bool
    last_run: datetime | None
    next_run: datetime | None
    created_at: datetime
    updated_at: datetime

