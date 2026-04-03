from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class ProviderDefinition(TimestampMixin, Base):
    __tablename__ = "providers"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    runtime_provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    auth_type: Mapped[str] = mapped_column(String(32), nullable=False, default="api_key")
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    default_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    docs_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    secret_placeholder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supports_secret_ref: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_custom_base_url: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
