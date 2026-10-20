import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from hermeshq.schemas.common import ORMModel

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class WidgetTheme(str, Enum):
    auto = "auto"
    light = "light"
    dark = "dark"


class WidgetPosition(str, Enum):
    right = "right"
    left = "left"


def _validate_hex_color(v: str) -> str:
    if not _HEX_COLOR_RE.match(v):
        raise ValueError("Must be a valid hex color (e.g. #6366f1)")
    return v


def _sanitize_title(v: str | None) -> str | None:
    if v is None:
        return None
    v = re.sub(r"[<>\"'&]", "", v)
    return v[:100] if v else None


class CreateSessionRequest(BaseModel):
    sender_id: str | None = Field(default=None, max_length=255)


class CreateSessionResponse(BaseModel):
    session_id: str
    session_token: str
    agent_name: str


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)


class CloseSessionRequest(BaseModel):
    session_token: str


class SessionStatusResponse(BaseModel):
    session_id: str
    status: str
    created_at: datetime
    last_activity: datetime
    message_count: int


class PublicChatMessageRead(BaseModel):
    role: str
    content: str
    created_at: datetime


class CreateApiKeyRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=255)
    agent_id: str
    allowed_domains: list[str] = Field(default_factory=list)
    requests_per_month: int = Field(default=1000, ge=1, le=1_000_000)
    tokens_per_month: int = Field(default=100_000, ge=1, le=100_000_000)
    widget_title: str | None = None
    widget_theme: WidgetTheme = WidgetTheme.auto
    widget_accent: str = "#6366f1"
    widget_position: WidgetPosition = WidgetPosition.right
    bound_user_id: str | None = None
    trust_sender_id: bool = False

    @field_validator("widget_accent")
    @classmethod
    def validate_accent(cls, v: str) -> str:
        return _validate_hex_color(v)

    @field_validator("widget_title")
    @classmethod
    def sanitize_title(cls, v: str | None) -> str | None:
        return _sanitize_title(v)


_UPDATE_ALLOWED_FIELDS = frozenset(
    {
        "label",
        "allowed_domains",
        "requests_per_month",
        "tokens_per_month",
        "is_active",
        "widget_title",
        "widget_theme",
        "widget_accent",
        "widget_position",
        "bound_user_id",
        "trust_sender_id",
    }
)


class UpdateApiKeyRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=255)
    allowed_domains: list[str] | None = None
    requests_per_month: int | None = Field(default=None, ge=1, le=1_000_000)
    tokens_per_month: int | None = Field(default=None, ge=1, le=100_000_000)
    is_active: bool | None = None
    widget_title: str | None = None
    widget_theme: WidgetTheme | None = None
    widget_accent: str | None = None
    widget_position: WidgetPosition | None = None
    bound_user_id: str | None = None
    trust_sender_id: bool | None = None

    @field_validator("widget_accent")
    @classmethod
    def validate_accent(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_hex_color(v)
        return v

    @field_validator("widget_title")
    @classmethod
    def sanitize_title(cls, v: str | None) -> str | None:
        return _sanitize_title(v)

    def safe_update_dict(self) -> dict:
        """Return only whitelisted fields that were explicitly set."""
        data = self.model_dump(exclude_unset=True)
        return {k: v for k, v in data.items() if k in _UPDATE_ALLOWED_FIELDS}


class ApiKeyRead(ORMModel):
    id: str
    key_prefix: str
    label: str
    agent_id: str
    allowed_domains: list[str]
    requests_per_month: int
    tokens_per_month: int
    is_active: bool
    widget_title: str | None
    widget_theme: str
    widget_accent: str
    widget_position: str
    bound_user_id: str | None = None
    trust_sender_id: bool = False
    created_at: datetime


class ApiKeyCreatedResponse(BaseModel):
    id: str
    raw_key: str
    key_prefix: str
    label: str


class TranscriptRead(ORMModel):
    id: str
    session_id: str
    agent_id: str
    messages_json: list[dict]
    archived_at: datetime


class NotificationRead(BaseModel):
    id: str
    title: str
    body: str
    created_at: datetime
    sender_id: str | None = None
    task_id: str | None = None
    schedule_id: str | None = None
