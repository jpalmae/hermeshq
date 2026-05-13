"""OIDC Provider model — stores per-provider OIDC configuration in the database."""

from uuid import uuid4

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class OidcProvider(TimestampMixin, Base):
    __tablename__ = "oidc_providers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    client_id: Mapped[str] = mapped_column(String(512))
    client_secret: Mapped[str] = mapped_column(String(512))
    discovery_url: Mapped[str] = mapped_column(String(1024))
    scopes: Mapped[str] = mapped_column(String(512), default="openid profile email")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_provision: Mapped[bool] = mapped_column(Boolean, default=False)
    allowed_domains: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
