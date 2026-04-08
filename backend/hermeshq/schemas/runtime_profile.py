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

