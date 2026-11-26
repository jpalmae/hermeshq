from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, utcnow

ATTEMPT_STATES = ("pending", "running", "completed", "failed", "cancelled", "lost", "abandoned")
LIVE_ATTEMPT_STATES = ("pending", "running")


class TaskExecutionAttempt(Base):
    __tablename__ = "task_execution_attempts"

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    execution_id: Mapped[str] = mapped_column(String(36), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_generation: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runner_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    runner_heartbeat_seq: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tool_events: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reconciliation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'running', 'completed', 'failed', 'cancelled', 'lost', 'abandoned')",
            name="ck_task_execution_attempts_state",
        ),
        Index(
            "uq_task_execution_attempts_live_task",
            "task_id",
            unique=True,
            postgresql_where=text("state IN ('pending', 'running')"),
        ),
    )


EXTERNAL_ACTION_STATES = ("iniciada", "confirmada", "no_ejecutada", "conciliada_ejecutada", "conciliada_no_ejecutada")


class ExternalActionRecord(Base):
    __tablename__ = "external_action_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[str] = mapped_column(String(36), index=True)
    lease_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(160))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(32), default="iniciada")
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('iniciada', 'confirmada', 'no_ejecutada', 'conciliada_ejecutada', 'conciliada_no_ejecutada')",
            name="ck_external_action_records_state",
        ),
    )
