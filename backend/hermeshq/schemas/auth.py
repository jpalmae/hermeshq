from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from hermeshq.schemas.common import ORMModel
from hermeshq.schemas.user_management import _validate_password_strength


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime


class UserRead(ORMModel):
    id: str
    username: str
    display_name: str
    role: str
    is_active: bool
    theme_preference: str
    avatar_url: str | None = None
    has_avatar: bool = False


class UserPreferencesUpdate(BaseModel):
    theme_preference: str


class UserProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        _validate_password_strength(value)
        return value
