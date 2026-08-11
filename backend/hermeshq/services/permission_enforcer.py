import fnmatch
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.permission_policy import PermissionPolicy

logger = logging.getLogger(__name__)


class PermissionEnforcer:
    """Evaluates permission policies for Pi agent tool calls."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def get_policy(self, agent: Agent) -> PermissionPolicy | None:
        if not agent.permission_policy_id:
            return None
        async with self.session_factory() as session:
            return await session.get(PermissionPolicy, agent.permission_policy_id)

    async def evaluate(
        self,
        agent: Agent,
        tool_name: str,
        tool_input: dict,
    ) -> tuple[bool, str | None, bool]:
        """Returns (allowed, reason_if_blocked, requires_approval)."""
        policy = await self.get_policy(agent)
        if not policy:
            return True, None, False

        allowed, reason = self._check_tool_rules(policy, tool_name)
        if not allowed:
            return False, reason, False

        if tool_name in ("bash", "shell", "terminal"):
            cmd = tool_input.get("command", "")
            blocked = self._check_command_rules(policy, cmd)
            if blocked:
                return False, f"Command blocked by policy '{policy.name}'", False
            if self._requires_approval(policy, tool_name, cmd):
                return True, None, True

        if tool_name in ("read", "write", "edit", "file"):
            path = tool_input.get("path", "")
            blocked = self._check_path_rules(policy, path)
            if blocked:
                return False, f"Path protected by policy '{policy.name}'", False
            if self._requires_approval(policy, tool_name, path):
                return True, None, True

        return True, None, False

    def _check_tool_rules(self, policy: PermissionPolicy, tool_name: str) -> tuple[bool, str | None]:
        rules = policy.tool_rules or {}
        deny = rules.get("deny", [])
        for pattern in deny:
            if pattern == "*" or fnmatch.fnmatch(tool_name, pattern):
                return False, f"Tool '{tool_name}' denied by policy '{policy.name}'"
        allow = rules.get("allow", [])
        if not allow:
            return True, None
        for pattern in allow:
            if pattern == "*" or fnmatch.fnmatch(tool_name, pattern):
                return True, None
        return False, f"Tool '{tool_name}' not in allowlist of policy '{policy.name}'"

    def _check_command_rules(self, policy: PermissionPolicy, command: str) -> bool:
        rules = policy.command_rules or {}
        deny = rules.get("deny", [])
        for pattern in deny:
            if pattern == "*" or self._glob_match(command.strip(), pattern):
                return True
        return False

    def _check_path_rules(self, policy: PermissionPolicy, path: str) -> bool:
        rules = policy.path_rules or {}
        deny_paths = rules.get("deny_paths", [])
        for pattern in deny_paths:
            if self._glob_match(path, pattern):
                return True
        return False

    def _requires_approval(self, policy: PermissionPolicy, tool_name: str, value: str) -> bool:
        rules = policy.approval_rules or {}
        require_list = rules.get("require_approval_for", [])
        combined = f"{tool_name}:{value}"
        for pattern in require_list:
            if pattern == "*" or self._glob_match(combined, pattern) or self._glob_match(value, pattern):
                return True
        return False

    @staticmethod
    def _glob_match(text: str, pattern: str) -> bool:
        import re

        if "**" in pattern:
            # Convert glob ** to regex: ** matches any path including /
            regex = pattern.replace("**", "\x00DOUBLESTAR\x00")
            regex = re.escape(regex)
            regex = regex.replace("\x00DOUBLESTAR\x00", ".*")
            # fnmatch-style single * (not crossing /)
            regex = regex.replace(re.escape("*"), "[^/]*")
            regex = regex.replace(re.escape("?"), ".")
            return bool(re.match("^" + regex + "$", text))
        return fnmatch.fnmatch(text, pattern)
