from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from hermeshq.models.base import Base, TimestampMixin


class AppSettings(TimestampMixin, Base):
    __tablename__ = "app_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="default")
    app_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    app_short_name: Mapped[str | None] = mapped_column(String(48), nullable=True)
    theme_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    default_locale: Mapped[str | None] = mapped_column(String(8), nullable=True)
    default_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_api_key_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    default_tui_skin: Mapped[str | None] = mapped_column(String(128), nullable=True)
    enabled_integration_packages: Mapped[list[str]] = mapped_column(JSON, default=list)
    tui_skin_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    logo_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    favicon_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
