from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermeshq.models.base import Base, utcnow


def default_board_order() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    source_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    prompt: Mapped[str] = mapped_column(Text)
    system_override: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    board_column: Mapped[str] = mapped_column(String(32), default="inbox", index=True)
    board_order: Mapped[int] = mapped_column(BigInteger, default=default_board_order, index=True)
    board_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=5)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    messages_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    tool_calls: Mapped[list[dict]] = mapped_column(JSON, default=list)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    agent = relationship("Agent", back_populates="tasks", foreign_keys=[agent_id])
