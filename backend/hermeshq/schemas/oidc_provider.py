"""Pydantic schemas for OIDC Provider CRUD."""

from pydantic import BaseModel, Field


class OidcProviderCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=128)
    client_id: str = Field(min_length=1, max_length=512)
    client_secret: str = Field(min_length=1, max_length=512)
    discovery_url: str = Field(min_length=1, max_length=1024)
    scopes: str = Field(default="openid profile email", max_length=512)
    enabled: bool = True
    auto_provision: bool = False
    allowed_domains: str | None = None
    icon_slug: str | None = None


class OidcProviderUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=128)
    client_id: str | None = Field(None, min_length=1, max_length=512)
    client_secret: str | None = Field(None, min_length=1, max_length=512)
    discovery_url: str | None = Field(None, min_length=1, max_length=1024)
    scopes: str | None = Field(None, max_length=512)
    enabled: bool | None = None
    auto_provision: bool | None = None
    allowed_domains: str | None = None
    icon_slug: str | None = None


class OidcProviderRead(BaseModel):
    id: str
    slug: str
    name: str
    client_id: str
    discovery_url: str
    scopes: str
    enabled: bool
    auto_provision: bool
    allowed_domains: str | None = None
    icon_slug: str | None = None

    model_config = {"from_attributes": True}


class OidcProviderReadSafe(BaseModel):
    """Public-facing schema — no secrets."""
    slug: str
    name: str
    enabled: bool
    icon_slug: str | None = None

    model_config = {"from_attributes": True}
