from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

_EVALUATE_TIMEOUT_SECONDS = 5.0

_BLOCKED_TOOLS_CACHE: dict[tuple[str, str], bool] = {}
_TOOL_NAME_CACHE: dict[str, str] = {}


def _internal_api_url() -> str:
    return os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")


def _evaluate(tool: str, tool_input: dict) -> dict | None:
    api_url = _internal_api_url()
    agent_id = os.environ.get("HERMESHQ_AGENT_ID", "")
    agent_token = os.environ.get("HERMESHQ_AGENT_TOKEN", "")
    if not api_url or not agent_id or not agent_token:
        return None
    payload = json.dumps({"tool": tool, "input": tool_input}).encode()
    request = urllib.request.Request(
        f"{api_url}/control/permissions/evaluate",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-HermesHQ-Agent-ID": agent_id,
            "X-HermesHQ-Agent-Token": agent_token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_EVALUATE_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"[hermeshq_guard] policy evaluation unavailable (fail-open): {exc}", flush=True)
        return None
    if not body.get("allowed", True):
        reason = body.get("reason") or "denied by permission policy"
        policy = body.get("policy_name") or "policy"
        return {"action": "block", "message": f"[hermeshq_guard] Blocked by HermesHQ policy '{policy}': {reason}"}
    return None


def _normalize_tool_name(tool_name: str) -> str:
    normalized = _TOOL_NAME_CACHE.get(tool_name)
    if normalized is None:
        normalized = tool_name.strip().lower()
        _TOOL_NAME_CACHE[tool_name] = normalized
    return normalized


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> dict | None:
    if not tool_name:
        return None
    tool_input = args if isinstance(args, dict) else {"value": args}
    return _evaluate(_normalize_tool_name(tool_name), tool_input)


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
