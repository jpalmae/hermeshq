from datetime import datetime

from pydantic import BaseModel, Field

from hermeshq.schemas.common import ORMModel


class MessagingChannelUpdate(BaseModel):
    enabled: bool = False
    mode: str = "bidirectional"
    secret_ref: str | None = None
    allowed_user_ids: list[str] = Field(default_factory=list)
    home_chat_id: str | None = None
    home_chat_name: str | None = None
    require_mention: bool = False
    free_response_chat_ids: list[str] = Field(default_factory=list)
    unauthorized_dm_behavior: str = "pair"


class MessagingChannelRead(ORMModel):
    id: str
    agent_id: str
    platform: str
    enabled: bool
    mode: str
    secret_ref: str | None
    allowed_user_ids: list[str]
    home_chat_id: str | None
    home_chat_name: str | None
    require_mention: bool
    free_response_chat_ids: list[str]
    unauthorized_dm_behavior: str
    status: str
    last_error: str | None
    metadata_json: dict
    created_at: datetime
    updated_at: datetime


class MessagingChannelRuntimeRead(BaseModel):
    status: str
    pid: int | None = None
    log_path: str | None = None
    last_bootstrap_at: datetime | None = None
    last_bootstrap_success_at: datetime | None = None
    last_bootstrap_status: str | None = None
    last_bootstrap_error: str | None = None
    last_bootstrap_duration_ms: int | None = None
    last_bootstrap_attempts: int | None = None
