from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "HermesHQ"
    api_prefix: str = "/api"
    debug: bool = False
    auth_mode: str = "local"

    database_url: str = "postgresql+asyncpg://hermeshq:hermeshq@localhost:5432/hermeshq"
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 12
    fernet_key: str | None = None
    oidc_issuer_url: str | None = None
    oidc_discovery_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_redirect_uri: str | None = None
    oidc_scope: str = "openid profile email"
    oidc_provider_name: str = "Authentik"
    oidc_provider_slug: str = "authentik"
    oidc_post_logout_redirect_uri: str | None = None
    oidc_auto_provision_users: bool = False
    oidc_visible_providers: str = ""
    oidc_provider_login_url_google: str | None = None
    oidc_provider_login_url_microsoft: str | None = None
    oidc_provider_login_url_authentik: str | None = None

    admin_username: str = "admin"
    admin_password: str = "admin123"
    admin_display_name: str = "Hermes Operator"

    workspaces_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "workspaces"
    )
    branding_root: Path | None = None
    hermes_skins_root: Path | None = None
    agent_assets_root: Path | None = None
    user_assets_root: Path | None = None
    integration_packages_root: Path | None = None
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:3420"]
    pty_shell: str = "/bin/sh"
    internal_api_base_url: str = "http://127.0.0.1:8000/api/internal"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def model_post_init(self, __context) -> None:
        self.workspaces_root = self.workspaces_root.resolve()
        if self.branding_root is None:
            self.branding_root = self.workspaces_root / "_branding"
        if self.hermes_skins_root is None:
            self.hermes_skins_root = self.workspaces_root / "_hermes_skins"
        if self.agent_assets_root is None:
            self.agent_assets_root = self.workspaces_root / "_agent_assets"
        if self.user_assets_root is None:
            self.user_assets_root = self.workspaces_root / "_user_assets"
        if self.integration_packages_root is None:
            self.integration_packages_root = self.workspaces_root / "_integration_packages"
        self.branding_root = self.branding_root.resolve()
        self.hermes_skins_root = self.hermes_skins_root.resolve()
        self.agent_assets_root = self.agent_assets_root.resolve()
        self.user_assets_root = self.user_assets_root.resolve()
        self.integration_packages_root = self.integration_packages_root.resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
