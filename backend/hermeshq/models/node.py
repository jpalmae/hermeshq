from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermeshq.models.base import Base, TimestampMixin


class Node(TimestampMixin, Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255))
    node_type: Mapped[str] = mapped_column(String(32), default="local")
    status: Mapped[str] = mapped_column(String(20), default="online", index=True)
    ssh_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ssh_port: Mapped[int] = mapped_column(default=22)
    hermes_path: Mapped[str] = mapped_column(String(255), default="~/.hermes")
    max_agents: Mapped[int] = mapped_column(default=10)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    system_info: Mapped[dict] = mapped_column(JSON, default=dict)

    agents = relationship("Agent", back_populates="node", cascade="all, delete-orphan")

