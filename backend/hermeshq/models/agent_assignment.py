from uuid import uuid4

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermeshq.models.base import Base, TimestampMixin


class AgentAssignment(TimestampMixin, Base):
    __tablename__ = "agent_assignments"
    __table_args__ = (UniqueConstraint("user_id", "agent_id", name="uq_agent_assignments_user_agent"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    assigned_by: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    user = relationship("User", foreign_keys=[user_id], back_populates="agent_assignments")
    agent = relationship("Agent", foreign_keys=[agent_id])
    assigned_by_user = relationship("User", foreign_keys=[assigned_by])
