from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from hermeshq.schemas.common import ORMModel


class AppSettingsUpdate(BaseModel):
    app_name: str | None = None
    app_short_name: str | None = None
    theme_mode: Literal["dark", "light", "system"] | None = None
    default_locale: Literal["en", "es"] | None = None
    default_provider: str | None = None
    default_model: str | None = None
    default_api_key_ref: str | None = None
    default_base_url: str | None = None


class AppSettingsRead(ORMModel):
    id: str
    app_name: str | None
    app_short_name: str | None
    theme_mode: Literal["dark", "light", "system"]
    default_locale: Literal["en", "es"]
    default_provider: str | None
    default_model: str | None
    default_api_key_ref: str | None
    default_base_url: str | None
    logo_url: str | None = None
    favicon_url: str | None = None
    has_logo: bool = False
    has_favicon: bool = False
    created_at: datetime
    updated_at: datetime
