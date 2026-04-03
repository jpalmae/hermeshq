from datetime import datetime

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel


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


class UserPreferencesUpdate(BaseModel):
    theme_preference: str
