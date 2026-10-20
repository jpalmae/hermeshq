from datetime import datetime

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel
from hermeshq.services.cron_schedule import DEFAULT_TIMEZONE


class ScheduledTaskCreate(BaseModel):
    agent_id: str
    name: str
    cron_expression: str
    prompt: str
    enabled: bool = True
    timezone: str = DEFAULT_TIMEZONE
    notify_public_chat: bool = False


class ScheduledTaskUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    prompt: str | None = None
    enabled: bool | None = None
    timezone: str | None = None
    notify_public_chat: bool | None = None


class ScheduledTaskApprove(BaseModel):
    cron_expression: str
    prompt: str


class ScheduledTaskRead(ORMModel):
    id: str
    agent_id: str
    name: str
    cron_expression: str
    prompt: str
    enabled: bool
    timezone: str
    owner_user_id: str | None
    approval_status: str
    created_by_agent_id: str | None
    notify_public_chat: bool
    last_run: datetime | None
    next_run: datetime | None
    created_at: datetime
    updated_at: datetime
