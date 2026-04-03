import re

from pydantic import BaseModel, Field, field_validator

from hermeshq.schemas.common import ORMModel


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field(default="user", pattern="^(admin|user)$")
    is_active: bool = True
    assigned_agent_ids: list[str] = Field(default_factory=list)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        _validate_password_strength(value)
        return value


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    password: str | None = Field(default=None, min_length=8, max_length=256)
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    is_active: bool | None = None
    assigned_agent_ids: list[str] | None = None

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str | None) -> str | None:
        if value is None:
            return value
        _validate_password_strength(value)
        return value


class UserManagedRead(ORMModel):
    id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    assigned_agent_ids: list[str]
    avatar_url: str | None = None
    has_avatar: bool = False


def _validate_password_strength(value: str) -> None:
    if len(value) < 8:
        raise ValueError("Password must have at least 8 characters")
    if not re.search(r"[A-Z]", value):
        raise ValueError("Password must include at least one uppercase letter")
    if not re.search(r"[0-9]", value):
        raise ValueError("Password must include at least one number")
    if not re.search(r"[^A-Za-z0-9]", value):
        raise ValueError("Password must include at least one special character")
