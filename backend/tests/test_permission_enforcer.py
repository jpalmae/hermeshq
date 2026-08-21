"""Tests for PermissionEnforcer — tool/path/command/network/approval rules."""

import pytest
from unittest.mock import MagicMock

from hermeshq.models.agent import Agent
from hermeshq.models.permission_policy import PermissionPolicy
from hermeshq.services.permission_enforcer import PermissionEnforcer


def _agent(agent_id: str = "agent-1") -> MagicMock:
    m = MagicMock(spec=Agent)
    m.id = agent_id
    m.permission_policy_id = None
    return m


def _policy(**kwargs):
    defaults = {
        "id": "test-id",
        "name": "Test Policy",
        "tool_rules": {"allow": ["*"], "deny": []},
        "path_rules": {"allow_paths": ["/workspace/**"], "deny_paths": []},
        "command_rules": {"allow": [], "deny": []},
        "network_rules": {"deny_all": False, "allow_domains": []},
        "approval_rules": {"require_approval_for": [], "auto_approve_threshold": "medium"},
        "is_system": False,
    }
    defaults.update(kwargs)
    return PermissionPolicy(**defaults)


class TestToolRules:
    def test_allow_star_passes(self):
        p = _policy(tool_rules={"allow": ["*"], "deny": []})
        e = PermissionEnforcer(None)
        decision = e.evaluate_policy(_agent(), p, "read", {})
        assert decision.allowed is True

    def test_allowlist_blocks_nonlisted(self):
        p = _policy(tool_rules={"allow": ["read", "bash"], "deny": []})
        e = PermissionEnforcer(None)
        decision = e.evaluate_policy(_agent(), p, "write", {})
        assert decision.allowed is False
        assert "not in allowlist" in decision.reason

    def test_deny_blocks(self):
        p = _policy(tool_rules={"allow": ["*"], "deny": ["terminal"]})
        e = PermissionEnforcer(None)
        decision = e.evaluate_policy(_agent(), p, "terminal", {})
        assert decision.allowed is False
        assert "denied" in decision.reason

    def test_deny_star_blocks_all(self):
        p = _policy(tool_rules={"allow": ["*"], "deny": ["*"]})
        e = PermissionEnforcer(None)
        decision = e.evaluate_policy(_agent(), p, "read", {})
        assert decision.allowed is False


class TestCommandRules:
    def test_block_pattern_matches(self):
        p = _policy(command_rules={"deny": ["rm -rf /", "sudo *"]})
        e = PermissionEnforcer(None)
        d = e.evaluate_policy(_agent(), p, "bash", {"command": "rm -rf /"})
        assert d.allowed is False
        d2 = e.evaluate_policy(_agent(), p, "bash", {"command": "sudo ls"})
        assert d2.allowed is False

    def test_block_pattern_miss(self):
        p = _policy(command_rules={"deny": ["rm -rf /"]})
        e = PermissionEnforcer(None)
        d = e.evaluate_policy(_agent(), p, "bash", {"command": "ls -la"})
        assert d.allowed is True

    def test_star_deny_blocks_all_commands(self):
        p = _policy(command_rules={"deny": ["*"]})
        e = PermissionEnforcer(None)
        d = e.evaluate_policy(_agent(), p, "bash", {"command": "anything"})
        assert d.allowed is False


class TestPathRules:
    def test_double_star_blocks(self):
        p = _policy(path_rules={"allow_paths": ["*"], "deny_paths": ["**/.env"]})
        e = PermissionEnforcer(None)
        d = e.evaluate_policy(_agent(), p, "read", {"path": "/app/.env"})
        assert d.allowed is False
        # Does NOT block workspace file that matches allow_paths
        d2 = e.evaluate_policy(_agent(), p, "read", {"path": "/workspace/file.txt"})
        assert d2.allowed is True

    def test_path_allow_and_deny(self):
        p = _policy(path_rules={"allow_paths": ["/workspace/**"], "deny_paths": ["/etc/**"]})
        e = PermissionEnforcer(None)
        d = e.evaluate_policy(_agent(), p, "read", {"path": "/etc/passwd"})
        assert d.allowed is False


class TestRequiresApproval:
    def test_requires_for_match(self):
        p = _policy(approval_rules={"require_approval_for": ["bash:sudo *"]})
        e = PermissionEnforcer(None)
        d = e.evaluate_policy(_agent("a1"), p, "bash", {"command": "sudo ls"})
        assert d.requires_approval is True

    def test_requires_no_match(self):
        p = _policy(approval_rules={"require_approval_for": ["bash:sudo *"]})
        e = PermissionEnforcer(None)
        d = e.evaluate_policy(_agent("a1"), p, "bash", {"command": "ls"})
        # ls alone does not match bash:sudo *
        assert d.requires_approval is False


class TestEvaluateWithNoPolicy:
    def test_no_policy_allows(self):
        e = PermissionEnforcer(None)
        agent = _agent()
        d = e.evaluate_policy(agent, None, "bash", {"command": "rm -rf /"})
        assert d.allowed is True

    def test_empty_tool_blocked(self):
        p = _policy()
        e = PermissionEnforcer(None)
        d = e.evaluate_policy(_agent(), p, "", {})
        assert d.allowed is False
