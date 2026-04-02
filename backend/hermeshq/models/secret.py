from uuid import uuid4

from sqlalchemy import LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class Secret(TimestampMixin, Base):
    __tablename__ = "secrets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value_enc: Mapped[bytes] = mapped_column(LargeBinary)

