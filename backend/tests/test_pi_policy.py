from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from hermeshq.schemas.permission_policy import PermissionPolicyCreate
from hermeshq.services.permission_enforcer import PermissionDecision, PermissionEnforcer


def agent(**overrides):
    values = {
        "id": "11111111-1111-4111-8111-111111111111",
        "permission_policy_id": "policy-id",
        "approval_mode": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def policy(**overrides):
    values = {
        "name": "Test policy",
        "tool_rules": {"allow": ["*"], "deny": []},
        "path_rules": {"allow_paths": ["/workspace/**"], "deny_paths": []},
        "command_rules": {"allow": [], "deny": []},
        "network_rules": {"deny_all": False, "allow_domains": []},
        "approval_rules": {"require_approval_for": []},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_policy_without_assignment_allows_tool_call() -> None:
    enforcer = PermissionEnforcer(None)

    decision = enforcer.evaluate_policy(agent(), None, "bash", {"command": "echo ok"})

    assert decision == PermissionDecision(allowed=True)


def test_malformed_policy_fails_closed() -> None:
    enforcer = PermissionEnforcer(None)
    current_policy = policy(tool_rules={"allow": "bash", "deny": []})

    decision = enforcer.evaluate_policy(agent(), current_policy, "bash", {"command": "echo ok"})

    assert decision.allowed is False
    assert "Invalid permission policy" in (decision.reason or "")


def test_policy_schema_rejects_malformed_rule_types() -> None:
    with pytest.raises(ValidationError, match="allow must be a list of strings"):
        PermissionPolicyCreate(name="Invalid", tool_rules={"allow": "bash"})


def test_tool_rules_support_tool_value_patterns() -> None:
    enforcer = PermissionEnforcer(None)
    current_policy = policy(
        tool_rules={"allow": ["read", "write"], "deny": ["write:**/.env"]},
    )

    denied = enforcer.evaluate_policy(agent(), current_policy, "write", {"path": ".env"})
    allowed = enforcer.evaluate_policy(agent(), current_policy, "write", {"path": "src/app.py"})

    assert denied.allowed is False
    assert "denied" in (denied.reason or "")
    assert allowed.allowed is True


def test_path_rules_normalize_workspace_paths_and_block_escapes() -> None:
    enforcer = PermissionEnforcer(None)
    current_policy = policy(
        tool_rules={"allow": ["read"], "deny": []},
        path_rules={"allow_paths": ["/workspace/**"], "deny_paths": ["**/.env"]},
    )

    allowed = enforcer.evaluate_policy(agent(), current_policy, "read", {"path": "src/app.py"})
    protected = enforcer.evaluate_policy(agent(), current_policy, "read", {"path": ".env"})
    escaped = enforcer.evaluate_policy(agent(), current_policy, "read", {"path": "../../etc/passwd"})

    assert allowed.allowed is True
    assert protected.allowed is False
    assert escaped.allowed is False
    assert "/etc/passwd" in (escaped.reason or "")


def test_command_allowlist_and_denylist_are_both_applied() -> None:
    enforcer = PermissionEnforcer(None)
    current_policy = policy(
        tool_rules={"allow": ["bash"], "deny": []},
        command_rules={"allow": ["git *"], "deny": ["git push *"]},
    )

    allowed = enforcer.evaluate_policy(agent(), current_policy, "bash", {"command": "git status"})
    denied = enforcer.evaluate_policy(agent(), current_policy, "bash", {"command": "git push origin main"})
    not_allowed = enforcer.evaluate_policy(agent(), current_policy, "bash", {"command": "ls"})

    assert allowed.allowed is True
    assert denied.allowed is False
    assert not_allowed.allowed is False


def test_network_rules_allow_only_declared_destinations() -> None:
    enforcer = PermissionEnforcer(None)
    current_policy = policy(
        network_rules={"deny_all": True, "allow_domains": ["*.openai.com"]},
    )

    allowed = enforcer.evaluate_policy(
        agent(),
        current_policy,
        "bash",
        {"command": "curl https://api.openai.com/v1/models"},
    )
    denied = enforcer.evaluate_policy(
        agent(),
        current_policy,
        "bash",
        {"command": "curl https://example.com/data"},
    )
    unknown = enforcer.evaluate_policy(agent(), current_policy, "bash", {"command": "ssh server"})

    assert allowed.allowed is True
    assert denied.allowed is False
    assert unknown.allowed is False


def test_runtime_approval_resolution_is_centralized_and_fail_closed() -> None:
    enforcer = PermissionEnforcer(None)
    decision = PermissionDecision(
        allowed=True,
        requires_approval=True,
        policy_name="Test policy",
    )

    assert enforcer.apply_runtime_approval(decision, "off").allowed is True
    assert enforcer.apply_runtime_approval(decision, "on-failure").allowed is True
    assert enforcer.apply_runtime_approval(decision, "on_request").allowed is False
    assert enforcer.apply_runtime_approval(decision, "unexpected").allowed is False
