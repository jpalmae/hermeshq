from pydantic import BaseModel


class RuntimeProfileDefaultsRead(BaseModel):
    enabled_toolsets: list[str]
    disabled_toolsets: list[str]
    max_iterations: int
    auto_approve_cmds: bool
    command_allowlist: list[str]


class RuntimeProfileRead(BaseModel):
    slug: str
    name: str
    description: str
    typical_roles: list[str]
    tooling_summary: str
    container_intent: str
    defaults: RuntimeProfileDefaultsRead


class RuntimeToolsetRead(BaseModel):
    slug: str
    name: str
    description: str


class RuntimePlatformPluginRead(BaseModel):
    slug: str
    name: str
    toolset: str
    description: str
    standard_compatible: bool


class RuntimeProfileCapabilityRead(BaseModel):
    slug: str
    name: str
    description: str
    tooling_summary: str
    container_intent: str
    terminal_allowed: bool
    phase1_full_access: bool
    builtin_toolsets: list[RuntimeToolsetRead]


class RuntimeCapabilityOverviewRead(BaseModel):
    profiles: list[RuntimeProfileCapabilityRead]
    platform_plugins: list[RuntimePlatformPluginRead]
