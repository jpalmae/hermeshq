from uuid import uuid4

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermeshq.models.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    agent_assignments = relationship(
        "AgentAssignment",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="AgentAssignment.user_id",
    )
