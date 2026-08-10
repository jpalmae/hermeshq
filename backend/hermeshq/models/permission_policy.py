from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class PermissionPolicy(TimestampMixin, Base):
    __tablename__ = "permission_policies"

    id: Mapped[str] = mapped_column(primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    path_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    command_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    network_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    approval_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
