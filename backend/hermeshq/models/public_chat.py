from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin, utcnow


class PublicChatApiKey(TimestampMixin, Base):
    __tablename__ = "public_chat_api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    allowed_domains: Mapped[list[str]] = mapped_column(ARRAY(String), default=list)
    requests_per_month: Mapped[int] = mapped_column(Integer, default=1000)
    tokens_per_month: Mapped[int] = mapped_column(Integer, default=100_000)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    widget_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    widget_theme: Mapped[str] = mapped_column(String(10), default="auto")
    widget_accent: Mapped[str] = mapped_column(String(10), default="#6366f1")
    widget_position: Mapped[str] = mapped_column(String(10), default="right")
    bound_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    trust_sender_id: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    last_notification_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PublicChatSession(TimestampMixin, Base):
    __tablename__ = "public_chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    api_key_id: Mapped[str] = mapped_column(ForeignKey("public_chat_api_keys.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    session_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    last_activity: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ttl_minutes: Mapped[int] = mapped_column(Integer, default=10)
    sender_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class PublicChatMessage(TimestampMixin, Base):
    __tablename__ = "public_chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("public_chat_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)


class PublicChatTranscript(Base):
    __tablename__ = "public_chat_transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    api_key_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    messages_json: Mapped[list[dict]] = mapped_column(JSON, default=list)
    archived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PublicChatNotification(Base):
    __tablename__ = "public_chat_notifications"
    __table_args__ = (Index("ix_public_chat_notifications_key_status", "api_key_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    api_key_id: Mapped[str] = mapped_column(ForeignKey("public_chat_api_keys.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    recipient_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    sender_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    schedule_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
