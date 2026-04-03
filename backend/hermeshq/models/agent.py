from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from hermeshq.models.base import Base, TimestampMixin


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    node_id: Mapped[str] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    friendly_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="stopped", index=True)
    run_mode: Mapped[str] = mapped_column(String(20), default="hybrid")
    model: Mapped[str] = mapped_column(String(255), default="anthropic/claude-sonnet-4")
    provider: Mapped[str] = mapped_column(String(64), default="openrouter")
    api_key_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled_toolsets: Mapped[list[str]] = mapped_column(JSON, default=list)
    disabled_toolsets: Mapped[list[str]] = mapped_column(JSON, default=list)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    mcp_servers: Mapped[list[dict]] = mapped_column(JSON, default=list)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    soul_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    personality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    context_files: Mapped[list[dict]] = mapped_column(JSON, default=list)
    workspace_path: Mapped[str] = mapped_column(Text)
    working_directory: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_iterations: Mapped[int] = mapped_column(Integer, default=90)
    max_tokens_per_task: Mapped[int] = mapped_column(Integer, default=100000)
    auto_approve_cmds: Mapped[bool] = mapped_column(Boolean, default=False)
    command_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)
    can_receive_tasks: Mapped[bool] = mapped_column(Boolean, default=True)
    can_send_tasks: Mapped[bool] = mapped_column(Boolean, default=True)
    supervisor_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id"), nullable=True
    )
    team_tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    total_tasks: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    last_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    node = relationship("Node", back_populates="agents")
    tasks = relationship(
        "Task",
        back_populates="agent",
        cascade="all, delete-orphan",
        foreign_keys="Task.agent_id",
    )
    messaging_channels = relationship(
        "MessagingChannel",
        back_populates="agent",
        cascade="all, delete-orphan",
    )
