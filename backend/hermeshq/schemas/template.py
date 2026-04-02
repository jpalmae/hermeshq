from datetime import datetime

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel


class TemplateCreate(BaseModel):
    name: str
    description: str | None = None
    config: dict = {}


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    config: dict | None = None


class TemplateRead(ORMModel):
    id: str
    name: str
    description: str | None
    config: dict
    created_at: datetime
    updated_at: datetime

