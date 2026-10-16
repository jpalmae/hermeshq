from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class CapabilityRequest(TimestampMixin, Base):
    __tablename__ = "capability_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    requested_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    example: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[str | None] = mapped_column(Text, nullable=True)
    files_json: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="nuevo", server_default="nuevo", index=True)
    fingerprint: Mapped[str] = mapped_column(String(80), index=True)
    me_too_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
