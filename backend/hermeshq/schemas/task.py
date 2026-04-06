from datetime import datetime

from pydantic import BaseModel, Field

from hermeshq.schemas.common import ORMModel


class TaskCreate(BaseModel):
    agent_id: str
    title: str | None = None
    prompt: str
    system_override: str | None = None
    priority: int = 5
    metadata: dict = Field(default_factory=dict)


class TaskRead(ORMModel):
    id: str
    agent_id: str
    title: str | None
    prompt: str
    status: str
    priority: int
    response: str | None
    error_message: str | None
    messages_json: list[dict]
    tool_calls: list[dict]
    tokens_used: int
    iterations: int
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    metadata: dict = Field(validation_alias="metadata_json", serialization_alias="metadata")
