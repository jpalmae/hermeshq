from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.permission_policy import PermissionPolicy

_FILE_TOOLS = {"read", "write", "edit", "file", "grep", "find", "ls"}
_SHELL_TOOLS = {"bash", "shell", "terminal"}
_NETWORK_COMMAND_RE = re.compile(
    r"(?:^|[\s;&|])(?:curl|wget|nc|ncat|netcat|ssh|scp|sftp|ftp|telnet)(?:\s|$)"
    r"|(?:^|[\s;&|])git\s+(?:clone|fetch|pull|push|ls-remote)(?:\s|$)"
    r"|(?:^|[\s;&|])(?:pip|pip3|npm|pnpm|yarn)\s+(?:install|add|publish)(?:\s|$)",
    re.IGNORECASE,
)
_URL_HOST_RE = re.compile(r"(?:https?|ftp)://([^/:\s]+)", re.IGNORECASE)
_HOST_ARGUMENT_RE = re.compile(r"(?:^|\s)(?:[A-Za-z0-9._-]+@)?([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?::\d+)?(?:[/\s]|$)")


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    allowed: bool
    reason: str | None = None
    requires_approval: bool = False
    policy_name: str | None = None


class PermissionEnforcer:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None) -> None:
        self.session_factory = session_factory

    async def get_policy(self, agent: Agent) -> PermissionPolicy | None:
        if not agent.permission_policy_id or self.session_factory is None:
            return None
        async with self.session_factory() as session:
            return await session.get(PermissionPolicy, agent.permission_policy_id)

    async def evaluate(
        self,
        agent: Agent,
        tool_name: str,
        tool_input: dict,
    ) -> PermissionDecision:
        policy = await self.get_policy(agent)
        return self.evaluate_policy(agent, policy, tool_name, tool_input)

    def evaluate_policy(
        self,
        agent: Agent,
        policy: PermissionPolicy | None,
        tool_name: str,
        tool_input: dict,
    ) -> PermissionDecision:
        tool_name = tool_name.strip()
        if not tool_name:
            return PermissionDecision(False, "Tool name is required")
        if policy is None:
            return PermissionDecision(True)
        policy_error = self._validate_policy(policy)
        if policy_error:
            return PermissionDecision(
                False,
                f"Invalid permission policy '{policy.name}': {policy_error}",
                policy_name=policy.name,
            )

        value = self._tool_value(agent, tool_name, tool_input)
        tool_subjects = [tool_name]
        if value:
            tool_subjects.append(f"{tool_name}:{value}")

        allowed, reason = self._check_allow_deny(
            tool_subjects,
            (policy.tool_rules or {}).get("allow", []),
            (policy.tool_rules or {}).get("deny", []),
            f"Tool '{tool_name}'",
            policy.name,
        )
        if not allowed:
            return PermissionDecision(False, reason, policy_name=policy.name)

        if tool_name in _SHELL_TOOLS:
            command = str(tool_input.get("command") or "").strip()
            allowed, reason = self._check_allow_deny(
                [command],
                (policy.command_rules or {}).get("allow", []),
                (policy.command_rules or {}).get("deny", []),
                "Command",
                policy.name,
            )
            if not allowed:
                return PermissionDecision(False, reason, policy_name=policy.name)
            allowed, reason = self._check_network_rules(policy, command)
            if not allowed:
                return PermissionDecision(False, reason, policy_name=policy.name)

        if tool_name in _FILE_TOOLS:
            path = self._normalize_path(agent, self._path_input(tool_input))
            allowed, reason = self._check_allow_deny(
                [path],
                (policy.path_rules or {}).get("allow_paths", []),
                (policy.path_rules or {}).get("deny_paths", []),
                f"Path '{path}'",
                policy.name,
            )
            if not allowed:
                return PermissionDecision(False, reason, policy_name=policy.name)

        requires_approval = self._requires_approval(policy, tool_name, value)
        return PermissionDecision(True, requires_approval=requires_approval, policy_name=policy.name)

    @staticmethod
    def apply_runtime_approval(decision: PermissionDecision, approval_mode: str | None) -> PermissionDecision:
        if not decision.allowed or not decision.requires_approval:
            return decision
        mode = (approval_mode or "inherit").strip().lower().replace("_", "-")
        if mode in {"inherit", "off", "on-failure"}:
            return replace(decision, requires_approval=False)
        if mode == "on-request":
            return replace(
                decision,
                allowed=False,
                reason="Manual approval is required but no human approval channel is available",
            )
        return replace(
            decision,
            allowed=False,
            reason=f"Unsupported approval mode '{approval_mode}'",
        )

    def _check_allow_deny(
        self,
        subjects: list[str],
        allow: object,
        deny: object,
        label: str,
        policy_name: str,
    ) -> tuple[bool, str | None]:
        deny_patterns = self._patterns(deny)
        if any(self._matches(subject, pattern) for pattern in deny_patterns for subject in subjects):
            return False, f"{label} denied by policy '{policy_name}'"
        allow_patterns = self._patterns(allow)
        if allow_patterns and not any(
            self._matches(subject, pattern) for pattern in allow_patterns for subject in subjects
        ):
            return False, f"{label} not in allowlist of policy '{policy_name}'"
        return True, None

    def _check_network_rules(self, policy: PermissionPolicy, command: str) -> tuple[bool, str | None]:
        rules = policy.network_rules or {}
        if not rules.get("deny_all") or not _NETWORK_COMMAND_RE.search(command):
            return True, None
        allowed_domains = self._patterns(rules.get("allow_domains", []))
        hosts = {match.lower().rstrip(".") for match in _URL_HOST_RE.findall(command)}
        if not hosts:
            hosts.update(match.lower().rstrip(".") for match in _HOST_ARGUMENT_RE.findall(command))
        if (
            hosts
            and allowed_domains
            and all(any(self._matches(host, pattern.lower()) for pattern in allowed_domains) for host in hosts)
        ):
            return True, None
        return False, f"Network access denied by policy '{policy.name}'"

    def _requires_approval(self, policy: PermissionPolicy, tool_name: str, value: str) -> bool:
        patterns = self._patterns((policy.approval_rules or {}).get("require_approval_for", []))
        subjects = [tool_name, f"{tool_name}:{value}"]
        if value:
            subjects.append(value)
        return any(self._matches(subject, pattern) for pattern in patterns for subject in subjects)

    def _tool_value(self, agent: Agent, tool_name: str, tool_input: dict) -> str:
        if tool_name in _SHELL_TOOLS:
            return str(tool_input.get("command") or "").strip()
        if tool_name in _FILE_TOOLS:
            return self._normalize_path(agent, self._path_input(tool_input))
        action = tool_input.get("action")
        return str(action).strip() if action is not None else ""

    @staticmethod
    def _path_input(tool_input: dict) -> str:
        return str(tool_input.get("path") or tool_input.get("file_path") or ".")

    @staticmethod
    def _normalize_path(agent: Agent, value: str) -> str:
        value = value.replace("\\", "/").strip()
        workspace = f"/app/workspaces/agent-{agent.id}"
        if value == workspace or value.startswith(f"{workspace}/"):
            value = f"/workspace{value[len(workspace) :]}"
        elif not value.startswith("/"):
            value = f"/workspace/{value}"
        return posixpath.normpath(value)

    @staticmethod
    def _patterns(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(pattern) for pattern in value if isinstance(pattern, str) and pattern]

    @staticmethod
    def _validate_policy(policy: PermissionPolicy) -> str | None:
        list_fields = {
            "tool_rules": ("allow", "deny"),
            "path_rules": ("allow_paths", "deny_paths"),
            "command_rules": ("allow", "deny"),
            "network_rules": ("allow_domains",),
            "approval_rules": ("require_approval_for",),
        }
        for attribute, fields in list_fields.items():
            rules = getattr(policy, attribute, None)
            if rules is None:
                continue
            if not isinstance(rules, dict):
                return f"{attribute} must be an object"
            for field in fields:
                value = rules.get(field)
                if value is not None and (
                    not isinstance(value, list) or any(not isinstance(item, str) for item in value)
                ):
                    return f"{attribute}.{field} must be a list of strings"
        network_rules = policy.network_rules or {}
        if "deny_all" in network_rules and not isinstance(network_rules["deny_all"], bool):
            return "network_rules.deny_all must be a boolean"
        return None

    @staticmethod
    def _matches(text: str, pattern: str) -> bool:
        if pattern == "*":
            return True
        if pattern.endswith("/**") and text == pattern[:-3]:
            return True
        return fnmatch.fnmatchcase(text, pattern)
