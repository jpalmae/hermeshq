from datetime import datetime

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel

CAPABILITY_STATUSES = ("nuevo", "en_curso", "resuelto", "descartado")


class CapabilityRequestRead(ORMModel):
    id: str
    agent_id: str
    agent_name: str | None = None
    requested_by_user_id: str | None = None
    requested_by_name: str | None = None
    task_id: str | None = None
    title: str
    description: str
    example: str | None = None
    attempts: str | None = None
    files_json: list = []
    status: str
    me_too_count: int
    resolution: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CapabilityRequestUpdate(BaseModel):
    status: str | None = None
    resolution: str | None = None
