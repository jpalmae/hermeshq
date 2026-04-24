from pydantic import BaseModel


class HermesVersionRead(BaseModel):
    version: str
    release_tag: str | None = None
    description: str | None = None
    source: str
    installed: bool
    install_status: str
    installed_path: str | None = None
    detected_version: str | None = None
    version_matches_detected: bool | None = None
    detected_version_warning: str | None = None
    is_default: bool = False
    is_effective_default: bool = False
    in_use_by_agents: int = 0


class HermesVersionCreate(BaseModel):
    version: str
    release_tag: str | None = None
    description: str | None = None


class HermesVersionUpdate(BaseModel):
    release_tag: str | None = None
    description: str | None = None


class HermesUpstreamVersionRead(BaseModel):
    release_tag: str
    commit_sha: str
    detected_version: str | None = None
    catalog_versions: list[str] = []
    already_in_catalog: bool = False


class HermesUpstreamCatalogCreate(BaseModel):
    release_tag: str
    description: str | None = None
