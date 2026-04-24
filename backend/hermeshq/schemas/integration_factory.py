from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from hermeshq.schemas.common import ORMModel
from hermeshq.schemas.managed_integration import ManagedIntegrationRead


class IntegrationDraftFileRead(BaseModel):
    path: str
    size: int


class IntegrationDraftCheckRead(BaseModel):
    level: Literal["info", "warning", "error"]
    code: str
    message: str
    path: str | None = None


class IntegrationDraftValidationRead(BaseModel):
    valid: bool
    checks: list[IntegrationDraftCheckRead]
    validated_at: str | None = None


class IntegrationDraftRead(ORMModel):
    id: str
    slug: str
    name: str
    description: str
    version: str
    template: str
    status: str
    created_by_user_id: str | None = None
    created_by_agent_id: str | None = None
    plugin_slug: str | None = None
    skill_identifier: str | None = None
    standard_compatible: bool = True
    supported_profiles: list[str] = []
    files: list[IntegrationDraftFileRead] = []
    last_validation: IntegrationDraftValidationRead | None = None
    published_package_slug: str | None = None
    published_package_version: str | None = None
    published_at: str | None = None
    notes: str | None = None


class IntegrationDraftCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="")
    template: Literal["rest-api", "empty"] = "rest-api"
    version: str = Field(default="0.1.0", min_length=1, max_length=32)


class IntegrationDraftUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    version: str | None = Field(default=None, min_length=1, max_length=32)
    notes: str | None = None


class IntegrationDraftFileContentRead(BaseModel):
    path: str
    content: str


class IntegrationDraftFileUpdate(BaseModel):
    content: str


class IntegrationDraftPublishRead(BaseModel):
    draft: IntegrationDraftRead
    integration: ManagedIntegrationRead
