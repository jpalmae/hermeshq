from datetime import datetime

from pydantic import BaseModel, Field


class PermissionPolicyBase(BaseModel):
    name: str = Field(..., max_length=255)
    description: str | None = None
    tool_rules: dict = Field(default_factory=lambda: {"allow": ["*"], "deny": []})
    path_rules: dict = Field(default_factory=lambda: {"allow_paths": ["/workspace/**"], "deny_paths": []})
    command_rules: dict = Field(default_factory=lambda: {"allow": [], "deny": []})
    network_rules: dict = Field(default_factory=lambda: {"deny_all": False})
    approval_rules: dict = Field(default_factory=lambda: {"require_approval_for": [], "auto_approve_threshold": "medium"})


class PermissionPolicyCreate(PermissionPolicyBase):
    pass


class PermissionPolicyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    tool_rules: dict | None = None
    path_rules: dict | None = None
    command_rules: dict | None = None
    network_rules: dict | None = None
    approval_rules: dict | None = None


class PermissionPolicyRead(PermissionPolicyBase):
    id: str
    is_system: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PermissionTestRequest(BaseModel):
    tool: str
    input: dict = Field(default_factory=dict)


class PermissionTestResult(BaseModel):
    allowed: bool
    reason: str | None = None
    policy_name: str | None = None
    requires_approval: bool = False
