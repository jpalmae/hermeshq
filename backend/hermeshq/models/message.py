from uuid import uuid4

from sqlalchemy import ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class AgentMessage(TimestampMixin, Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    from_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    to_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True, index=True)
    message_type: Mapped[str] = mapped_column(String(20), default="direct")
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="delivered")
