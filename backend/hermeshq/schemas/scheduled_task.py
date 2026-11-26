from datetime import datetime

from pydantic import BaseModel, Field

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
    output_format: str | None = Field(default=None, max_length=4000)


class ScheduledTaskUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    prompt: str | None = None
    enabled: bool | None = None
    timezone: str | None = None
    notify_public_chat: bool | None = None
    output_format: str | None = Field(default=None, max_length=4000)


class ScheduledTaskApprove(BaseModel):
    cron_expression: str
    prompt: str
    output_format: str | None = None


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
    output_format: str | None = None
    definition_version: int = 1
    delivery_key_id: str | None = None
    last_run: datetime | None
    next_run: datetime | None
    created_at: datetime
    updated_at: datetime


class ScheduledTaskDelivery(BaseModel):
    delivery_key_id: str | None = None
