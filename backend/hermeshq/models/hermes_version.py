from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class HermesVersion(TimestampMixin, Base):
    __tablename__ = "hermes_versions"

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    release_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

