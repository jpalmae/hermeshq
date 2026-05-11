from datetime import datetime

from pydantic import BaseModel, Field

from hermeshq.schemas.common import ORMModel


MCP_SCOPE_PATTERN = "^(agents:list|agents:invoke|tasks:read)$"


class McpAccessTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    client_name: str | None = Field(default=None, max_length=128)
    allowed_agent_ids: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(
        default_factory=lambda: ["agents:list", "agents:invoke", "tasks:read"]
    )
    expires_at: datetime | None = None


class McpAccessTokenUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    client_name: str | None = Field(default=None, max_length=128)
    allowed_agent_ids: list[str] | None = None
    scopes: list[str] | None = None
    is_active: bool | None = None
    expires_at: datetime | None = None


class McpAccessTokenRead(ORMModel):
    id: str
    name: str
    description: str | None
    client_name: str | None
    token_prefix: str
    created_by_user_id: str | None
    allowed_agent_ids: list[str]
    scopes: list[str]
    is_active: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class McpAccessTokenCreateResult(BaseModel):
    token: str
    access: McpAccessTokenRead
