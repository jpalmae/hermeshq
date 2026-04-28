from pydantic import BaseModel


class ManagedIntegrationFieldRead(BaseModel):
    name: str
    label: str
    kind: str
    placeholder: str | None = None
    secret_provider: str | None = None
    options: list[str] = []


class ManagedIntegrationActionRead(BaseModel):
    slug: str
    label: str
    description: str | None = None


class ManagedIntegrationRead(BaseModel):
    slug: str
    name: str
    description: str
    version: str
    source_type: str
    installed: bool
    standard_compatible: bool
    supported_profiles: list[str]
    required_fields: list[str]
    fields: list[ManagedIntegrationFieldRead]
    defaults: dict[str, str]
    secret_provider: str | None = None
    plugin_slug: str | None = None
    plugin_name: str | None = None
    plugin_description: str | None = None
    skill_identifier: str | None = None
    test_action: str | None = None
    env_map: dict[str, str]
    tools: list[str]
    actions: list[ManagedIntegrationActionRead] = []


class ManagedIntegrationTestRequest(BaseModel):
    config: dict[str, str] | None = None


class ManagedIntegrationTestResult(BaseModel):
    success: bool
    message: str
    details: dict | None = None


class ManagedIntegrationActionRequest(BaseModel):
    config: dict[str, str] | None = None


class ManagedIntegrationActionResult(BaseModel):
    success: bool
    message: str
    details: dict | None = None
