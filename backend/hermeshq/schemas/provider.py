from datetime import datetime

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    description: str | None = None
    docs_url: str | None = None
    secret_placeholder: str | None = None
    supports_secret_ref: bool | None = None
    supports_custom_base_url: bool | None = None
    enabled: bool | None = None


class ProviderRead(ORMModel):
    slug: str
    name: str
    runtime_provider: str
    auth_type: str
    base_url: str | None
    default_model: str | None
    description: str | None
    docs_url: str | None
    secret_placeholder: str | None
    supports_secret_ref: bool
    supports_custom_base_url: bool
    enabled: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
