from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class CloudPlatform(TimestampMixin, Base):
    __tablename__ = "cloud_platforms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="vergeos")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    api_url: Mapped[str] = mapped_column(String(512), nullable=False)
    admin_credential_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    insecure_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version_info: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CloudTenant(TimestampMixin, Base):
    __tablename__ = "cloud_tenants"
    __table_args__ = (UniqueConstraint("platform_id", "tenant_ref", name="uq_cloud_tenant_platform_ref"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    platform_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cloud_platforms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    cache_ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CloudBinding(TimestampMixin, Base):
    __tablename__ = "cloud_bindings"
    __table_args__ = (UniqueConstraint("tenant_id", "agent_id", name="uq_cloud_binding_tenant_agent"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cloud_tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cloud_role: Mapped[str] = mapped_column(String(32), nullable=False, default="viewer")
