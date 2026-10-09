from uuid import uuid4

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermeshq.models.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255))
    auth_source: Mapped[str] = mapped_column(String(32), default="local", index=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(512), nullable=True, unique=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default="user", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    theme_preference: Mapped[str] = mapped_column(String(16), default="default")
    locale_preference: Mapped[str] = mapped_column(String(16), default="default")
    avatar_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)

    telegram_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    whatsapp_user: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    teams_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    google_chat_email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    kapso_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    kapso_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    agent_assignments = relationship(
        "AgentAssignment",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="AgentAssignment.user_id",
    )
    m365_token = relationship(
        "UserM365Token",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )
