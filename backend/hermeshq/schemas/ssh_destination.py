from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class SshDestinationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=22, ge=1, le=65535)
    listen_port: int = Field(ge=20000, le=29999)
    allowed_agent_id: str | None = None
    notes: str | None = Field(default=None, max_length=512)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(char.isspace() for char in cleaned):
            raise ValueError("Host must be a hostname or IP without spaces")
        return cleaned


class SshDestinationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    listen_port: int | None = Field(default=None, ge=20000, le=29999)
    allowed_agent_id: str | None = None
    active: bool | None = None
    notes: str | None = Field(default=None, max_length=512)


class SshDestinationRead(BaseModel):
    id: str
    name: str
    host: str
    port: int
    listen_port: int
    allowed_agent_id: str | None
    active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime
