from datetime import datetime

from pydantic import BaseModel, Field

from hermeshq.schemas.common import ORMModel


class MessageCreate(BaseModel):
    from_agent_id: str
    to_agent_id: str
    message_type: str = "direct"
    content: str
    metadata: dict = {}


class BroadcastCreate(BaseModel):
    from_agent_id: str
    team_tag: str
    content: str
    metadata: dict = {}


class MessageRead(ORMModel):
    id: str
    from_agent_id: str
    to_agent_id: str
    task_id: str | None
    message_type: str
    content: str
    metadata: dict = Field(validation_alias="metadata_json", serialization_alias="metadata")
    status: str
    created_at: datetime
    updated_at: datetime
