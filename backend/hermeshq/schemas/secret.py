from datetime import datetime

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel


class SecretCreate(BaseModel):
    name: str
    provider: str | None = None
    value: str


class SecretUpdate(BaseModel):
    provider: str | None = None
    value: str | None = None


class SecretRead(ORMModel):
    id: str
    name: str
    provider: str | None
    created_at: datetime
    updated_at: datetime

