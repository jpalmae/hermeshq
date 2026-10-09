from datetime import datetime

from pydantic import BaseModel, Field, field_validator


def _validate_rule_object(
    value: dict | None,
    list_fields: tuple[str, ...],
    boolean_fields: tuple[str, ...] = (),
) -> dict | None:
    if value is None:
        return value
    for field in list_fields:
        configured = value.get(field)
        if configured is not None and (
            not isinstance(configured, list) or any(not isinstance(item, str) for item in configured)
        ):
            raise ValueError(f"{field} must be a list of strings")
    for field in boolean_fields:
        configured = value.get(field)
        if configured is not None and not isinstance(configured, bool):
            raise ValueError(f"{field} must be a boolean")
    return value


class PermissionPolicyBase(BaseModel):
    name: str = Field(..., max_length=255)
    description: str | None = None
    tool_rules: dict = Field(default_factory=lambda: {"allow": ["*"], "deny": []})
    path_rules: dict = Field(default_factory=lambda: {"allow_paths": ["/workspace/**"], "deny_paths": []})
    command_rules: dict = Field(default_factory=lambda: {"allow": [], "deny": []})
    network_rules: dict = Field(default_factory=lambda: {"deny_all": False})
    approval_rules: dict = Field(
        default_factory=lambda: {"require_approval_for": [], "auto_approve_threshold": "medium"}
    )

    @field_validator("tool_rules", "command_rules")
    @classmethod
    def validate_allow_deny_rules(cls, value: dict) -> dict:
        return _validate_rule_object(value, ("allow", "deny")) or {}

    @field_validator("path_rules")
    @classmethod
    def validate_path_rules(cls, value: dict) -> dict:
        return _validate_rule_object(value, ("allow_paths", "deny_paths")) or {}

    @field_validator("network_rules")
    @classmethod
    def validate_network_rules(cls, value: dict) -> dict:
        return _validate_rule_object(value, ("allow_domains",), ("deny_all",)) or {}

    @field_validator("approval_rules")
    @classmethod
    def validate_approval_rules(cls, value: dict) -> dict:
        return _validate_rule_object(value, ("require_approval_for",)) or {}


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

    @field_validator("tool_rules", "command_rules")
    @classmethod
    def validate_allow_deny_rules(cls, value: dict | None) -> dict | None:
        return _validate_rule_object(value, ("allow", "deny"))

    @field_validator("path_rules")
    @classmethod
    def validate_path_rules(cls, value: dict | None) -> dict | None:
        return _validate_rule_object(value, ("allow_paths", "deny_paths"))

    @field_validator("network_rules")
    @classmethod
    def validate_network_rules(cls, value: dict | None) -> dict | None:
        return _validate_rule_object(value, ("allow_domains",), ("deny_all",))

    @field_validator("approval_rules")
    @classmethod
    def validate_approval_rules(cls, value: dict | None) -> dict | None:
        return _validate_rule_object(value, ("require_approval_for",))


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
