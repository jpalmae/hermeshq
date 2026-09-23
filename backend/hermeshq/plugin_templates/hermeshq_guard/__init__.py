from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_EVALUATE_TIMEOUT_SECONDS = 5.0
_LAST_OK_FILE = ".hermeshq_guard_last_ok"
_GRACE_STATE = {"last_ok": None}
_GUARD_USER_AGENT = "hermeshq-guard/1.0"


def _internal_api_url() -> str:
    return os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")


def _fail_mode() -> tuple[str, int]:
    mode = (os.environ.get("HERMESHQ_GUARD_FAIL_MODE") or "fail-open").strip()
    if mode == "fail-closed":
        return "fail-closed", 0
    if mode.startswith("fail-grace:"):
        try:
            return "fail-grace", max(0, int(mode.split(":", 1)[1]))
        except ValueError:
            pass
    return "fail-open", 0


def _state_dir() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home


def _load_last_ok() -> float | None:
    if _GRACE_STATE["last_ok"] is not None:
        return _GRACE_STATE["last_ok"]
    try:
        raw = (_state_dir() / _LAST_OK_FILE).read_text(encoding="utf-8").strip()
        value = float(raw)
        _GRACE_STATE["last_ok"] = value
        return value
    except (OSError, ValueError):
        return None


def _mark_ok() -> None:
    now = time.time()
    _GRACE_STATE["last_ok"] = now
    try:
        marker = _state_dir() / _LAST_OK_FILE
        marker.write_text(str(now), encoding="utf-8")
        os.chmod(marker, 0o600)
    except OSError:
        pass


def _offline_decision() -> dict | None:
    """Return a block decision when offline behavior demands it, else None (allow)."""
    mode, grace_seconds = _fail_mode()
    if mode == "fail-open":
        return None
    if mode == "fail-grace":
        last_ok = _load_last_ok()
        if last_ok is not None and (time.time() - last_ok) <= grace_seconds:
            return None
        return {
            "action": "block",
            "message": (
                f"[hermeshq_guard] Policy server unreachable beyond grace window "
                f"({grace_seconds}s) — blocking by fail-grace policy."
            ),
        }
    return {
        "action": "block",
        "message": "[hermeshq_guard] Policy server unreachable and fail-closed mode is active — blocking.",
    }


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
            "User-Agent": _GUARD_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_EVALUATE_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {
                "action": "block",
                "message": "[hermeshq_guard] Credentials rejected by HermesHQ — device may be revoked. Blocking.",
            }
        print(f"[hermeshq_guard] policy evaluation error (HTTP {exc.code}): applying offline behavior", flush=True)
        return _offline_decision()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"[hermeshq_guard] policy evaluation unavailable: applying offline behavior ({exc})", flush=True)
        return _offline_decision()
    _mark_ok()
    if not body.get("allowed", True):
        reason = body.get("reason") or "denied by permission policy"
        policy = body.get("policy_name") or "policy"
        return {"action": "block", "message": f"[hermeshq_guard] Blocked by HermesHQ policy '{policy}': {reason}"}
    return None


def _normalize_tool_name(tool_name: str) -> str:
    return tool_name.strip().lower()


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> dict | None:
    if not tool_name:
        return None
    tool_input = args if isinstance(args, dict) else {"value": args}
    return _evaluate(_normalize_tool_name(tool_name), tool_input)


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
