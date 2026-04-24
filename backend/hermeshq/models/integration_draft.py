from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class IntegrationDraft(TimestampMixin, Base):
    __tablename__ = "integration_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    template: Mapped[str] = mapped_column(String(32), default="rest-api", index=True)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    last_validation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    published_package_slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    published_package_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    published_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
