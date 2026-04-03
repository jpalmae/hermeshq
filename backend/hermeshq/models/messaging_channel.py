from uuid import uuid4

from sqlalchemy import Boolean, ForeignKey, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermeshq.models.base import Base, TimestampMixin


class MessagingChannel(TimestampMixin, Base):
    __tablename__ = "messaging_channels"
    __table_args__ = (
        UniqueConstraint("agent_id", "platform", name="uq_messaging_channels_agent_platform"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(32), default="bidirectional")
    secret_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    allowed_user_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    home_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    home_chat_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    require_mention: Mapped[bool] = mapped_column(Boolean, default=False)
    free_response_chat_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    unauthorized_dm_behavior: Mapped[str] = mapped_column(String(32), default="pair")
    status: Mapped[str] = mapped_column(String(20), default="stopped", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    agent = relationship("Agent", back_populates="messaging_channels")
