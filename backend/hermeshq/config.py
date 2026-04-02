from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "HermesHQ"
    api_prefix: str = "/api"
    debug: bool = False

    database_url: str = "postgresql+asyncpg://hermeshq:hermeshq@localhost:5432/hermeshq"
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 12
    fernet_key: str | None = None

    admin_username: str = "admin"
    admin_password: str = "admin123"
    admin_display_name: str = "Hermes Operator"

    workspaces_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "workspaces"
    )
    branding_root: Path | None = None
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3420"]
    pty_shell: str = "/bin/sh"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def model_post_init(self, __context) -> None:
        if self.branding_root is None:
            self.branding_root = self.workspaces_root / "_branding"


@lru_cache
def get_settings() -> Settings:
    return Settings()
