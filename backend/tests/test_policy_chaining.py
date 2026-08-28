"""Tests for policy chaining (Phase 4c) — deny-wins, allow-union semantics."""

from unittest.mock import MagicMock

from hermeshq.models.agent import Agent
from hermeshq.models.permission_policy import PermissionPolicy
from hermeshq.services.permission_enforcer import PermissionEnforcer


def _agent(primary: str | None = None, chained: list[str] | None = None) -> MagicMock:
    m = MagicMock(spec=Agent)
    m.id = "agent-1"
    m.permission_policy_id = primary
    m.permission_policy_ids = chained or []
    return m


def _policy(pid: str, name: str, **kwargs):
    defaults = {
        "tool_rules": {"allow": ["*"], "deny": []},
        "path_rules": {"allow_paths": [], "deny_paths": []},
        "command_rules": {"allow": [], "deny": []},
        "network_rules": {"allow_domains": [], "deny_all": False},
        "approval_rules": {"require_approval_for": [], "auto_approve_threshold": "medium"},
        "is_system": True,
    }
    defaults.update(kwargs)
    return PermissionPolicy(id=pid, name=name, **defaults)


DEVELOPER = _policy(
    "sys-pi-developer", "Pi Developer",
    tool_rules={"allow": ["read", "bash", "edit", "write", "grep", "find", "ls"], "deny": []},
    command_rules={"allow": [], "deny": ["rm -rf /"]},
    approval_rules={"require_approval_for": ["bash:sudo *"], "auto_approve_threshold": "medium"},
)

RESTRICTED_DOMAINS = _policy(
    "custom-1", "Corporate Domains",
    network_rules={"allow_domains": ["corp.example.com"], "deny_all": True},
    approval_rules={"require_approval_for": [], "auto_approve_threshold": "high"},
)


class TestMergePolicies:
    def test_single_policy_passthrough(self):
        e = PermissionEnforcer(None)
        merged = e._merge_policies([DEVELOPER])
        assert merged is DEVELOPER

    def test_empty_returns_none(self):
        e = PermissionEnforcer(None)
        assert e._merge_policies([]) is None

    def test_deny_union(self):
        sandbox = _policy(
            "sys-pi-sandboxed", "Pi Sandboxed",
            tool_rules={"allow": ["read", "bash", "edit"], "deny": []},
            command_rules={"allow": [], "deny": ["sudo *"]},
        )
        e = PermissionEnforcer(None)
        merged = e._merge_policies([DEVELOPER, sandbox])
        assert "rm -rf /" in merged.command_rules["deny"]
        assert "sudo *" in merged.command_rules["deny"]

    def test_allow_union(self):
        extra = _policy(
            "custom-2", "Extra Tools",
            tool_rules={"allow": ["web_search"], "deny": []},
        )
        e = PermissionEnforcer(None)
        merged = e._merge_policies([DEVELOPER, extra])
        allows = merged.tool_rules["allow"]
        assert "bash" in allows
        assert "web_search" in allows

    def test_network_deny_all_wins_and_intersects_domains(self):
        open_policy = _policy("open-1", "Open", network_rules={"allow_domains": [], "deny_all": False})
        e = PermissionEnforcer(None)
        merged = e._merge_policies([open_policy, RESTRICTED_DOMAINS])
        assert merged.network_rules["deny_all"] is True
        assert merged.network_rules["allow_domains"] == ["corp.example.com"]

    def test_two_blocking_policies_intersect_domains(self):
        other = _policy(
            "custom-3", "Other Domains",
            network_rules={"allow_domains": ["corp.example.com", "api.other.com"], "deny_all": True},
        )
        e = PermissionEnforcer(None)
        merged = e._merge_policies([RESTRICTED_DOMAINS, other])
        # intersection of [corp] and [corp, api.other] = [corp]
        assert merged.network_rules["allow_domains"] == ["corp.example.com"]

    def test_approval_union(self):
        extra = _policy(
            "custom-4", "More Approvals",
            approval_rules={"require_approval_for": ["bash:deploy *"], "auto_approve_threshold": "low"},
        )
        e = PermissionEnforcer(None)
        merged = e._merge_policies([DEVELOPER, extra])
        assert "bash:sudo *" in merged.approval_rules["require_approval_for"]
        assert "bash:deploy *" in merged.approval_rules["require_approval_for"]


class TestChainedEvaluation:
    def test_chained_deny_blocks_tool(self):
        blocker = _policy(
            "block-write", "No Write",
            tool_rules={"allow": [], "deny": ["write"]},
        )
        e = PermissionEnforcer(None)
        merged = e._merge_policies([DEVELOPER, blocker])
        decision = e.evaluate_policy(_agent(), merged, "write", {})
        assert decision.allowed is False

    def test_chained_extra_allow_extends(self):
        base = _policy(
            "read-only", "ReadOnly",
            tool_rules={"allow": ["read"], "deny": []},
        )
        ext = _policy(
            "ext", "Ext",
            tool_rules={"allow": ["grep"], "deny": []},
        )
        e = PermissionEnforcer(None)
        merged = e._merge_policies([base, ext])
        assert e.evaluate_policy(_agent(), merged, "grep", {}).allowed is True
        assert e.evaluate_policy(_agent(), merged, "read", {}).allowed is True
        assert e.evaluate_policy(_agent(), merged, "bash", {}).allowed is False
