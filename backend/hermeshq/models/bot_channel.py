from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class BotChannel(TimestampMixin, Base):
    __tablename__ = "bot_channels"
    __table_args__ = (Index("uq_bot_channels_default", "is_default", unique=True, postgresql_where=text("is_default")),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), default="microsoft_teams", server_default="microsoft_teams")
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    azure_app_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    service_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    require_channel_header: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class UserChannelIdentity(TimestampMixin, Base):
    __tablename__ = "user_channel_identities"
    __table_args__ = (
        UniqueConstraint("bot_channel_id", "external_user_id", name="uq_user_channel_identities_externo"),
        UniqueConstraint("bot_channel_id", "aad_object_id", "tenant_id", name="uq_user_channel_identities_aad"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    bot_channel_id: Mapped[str] = mapped_column(
        ForeignKey("bot_channels.id", ondelete="CASCADE"), index=True, nullable=False
    )
    external_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    aad_object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class PublicChatDeliveryPref(TimestampMixin, Base):
    __tablename__ = "public_chat_delivery_prefs"
    __table_args__ = (UniqueConstraint("user_id", "agent_id", name="uq_public_chat_delivery_prefs_par"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    cron_key_id: Mapped[str | None] = mapped_column(
        ForeignKey("public_chat_api_keys.id", ondelete="SET NULL"), nullable=True
    )
    notice_key_id: Mapped[str | None] = mapped_column(
        ForeignKey("public_chat_api_keys.id", ondelete="SET NULL"), nullable=True
    )
