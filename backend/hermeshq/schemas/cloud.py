from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from hermeshq.schemas.common import ORMModel


class CloudPlatformCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    api_url: str = Field(min_length=8, max_length=512)
    kind: str = Field(default="vergeos", pattern="^(vergeos)$")
    admin_credential_ref: str | None = None
    insecure_tls: bool = False

    @field_validator("api_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.startswith("https://") and not cleaned.startswith("http://"):
            raise ValueError("api_url must be an http(s) URL")
        return cleaned.rstrip("/")


class CloudPlatformUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    api_url: str | None = Field(default=None, min_length=8, max_length=512)
    admin_credential_ref: str | None = None
    insecure_tls: bool | None = None
    active: bool | None = None


class CloudPlatformRead(ORMModel):
    id: str
    kind: str
    name: str
    api_url: str
    admin_credential_ref: str | None
    insecure_tls: bool
    active: bool
    created_at: datetime
    updated_at: datetime


class CloudTenantCreate(BaseModel):
    platform_id: str
    tenant_ref: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=128)
    credential_ref: str | None = Field(default=None, max_length=128)
    cache_ttl_seconds: int = Field(default=30, ge=5, le=600)
    api_key: str | None = Field(default=None, min_length=8, max_length=512)

    @model_validator(mode="after")
    def validate_credentials(self) -> "CloudTenantCreate":
        if not self.credential_ref and not self.api_key:
            raise ValueError("credential_ref or api_key is required")
        return self


class CloudTenantUpdate(BaseModel):
    tenant_ref: str | None = Field(default=None, min_length=1, max_length=255)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    credential_ref: str | None = None
    api_key: str | None = Field(default=None, min_length=8, max_length=512)
    cache_ttl_seconds: int | None = Field(default=None, ge=5, le=600)
    active: bool | None = None


class CloudTenantRead(ORMModel):
    id: str
    platform_id: str
    tenant_ref: str
    display_name: str
    credential_ref: str
    cache_ttl_seconds: int
    active: bool
    created_at: datetime
    updated_at: datetime


class CloudBindingCreate(BaseModel):
    tenant_id: str
    agent_id: str
    cloud_role: str = Field(default="viewer", pattern="^(viewer|operator|admin)$")


class CloudBindingRead(ORMModel):
    id: str
    tenant_id: str
    agent_id: str
    cloud_role: str
    created_at: datetime
    updated_at: datetime


class CloudRequestIn(BaseModel):
    action: str = Field(min_length=3, max_length=64)
    params: dict = Field(default_factory=dict)
