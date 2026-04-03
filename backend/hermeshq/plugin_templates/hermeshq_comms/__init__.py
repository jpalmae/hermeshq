from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def _api_request(method: str, path: str, payload: dict | None = None) -> str:
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    agent_id = os.environ.get("HERMESHQ_AGENT_ID", "")
    agent_token = os.environ.get("HERMESHQ_AGENT_TOKEN", "")
    if not base_url or not agent_id or not agent_token:
        return json.dumps({"success": False, "error": "HermesHQ internal communication is not configured in this runtime"})

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method=method.upper(),
        headers={
            "Content-Type": "application/json",
            "X-HermesHQ-Agent-ID": agent_id,
            "X-HermesHQ-Agent-Token": agent_token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(body) if body else {}
        except Exception:
            parsed = {}
        return json.dumps(
            {
                "success": False,
                "status_code": exc.code,
                "error": parsed.get("detail") or parsed.get("error") or body or str(exc),
            }
        )
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)})


def hq_list_agents_tool(args, **_kwargs):
    return _api_request("GET", "/agents/self/roster")


def hq_direct_message_tool(args, **_kwargs):
    target_agent = (args.get("target_agent") or "").strip()
    message = (args.get("message") or "").strip()
    if not target_agent or not message:
        return json.dumps({"success": False, "error": "Both 'target_agent' and 'message' are required"})
    return _api_request(
        "POST",
        "/agents/self/direct",
        {
            "target_agent": target_agent,
            "content": message,
            "metadata": args.get("metadata") or {},
        },
    )


def hq_delegate_task_tool(args, **_kwargs):
    target_agent = (args.get("target_agent") or "").strip()
    instruction = (args.get("instruction") or "").strip()
    if not target_agent or not instruction:
        return json.dumps({"success": False, "error": "Both 'target_agent' and 'instruction' are required"})
    metadata = dict(args.get("metadata") or {})
    task_id = _kwargs.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        metadata.setdefault("parent_task_id", task_id.strip())
    session_platform = os.environ.get("HERMES_SESSION_PLATFORM", "").strip().lower()
    session_chat_id = os.environ.get("HERMES_SESSION_CHAT_ID", "").strip()
    session_thread_id = os.environ.get("HERMES_SESSION_THREAD_ID", "").strip()
    if session_platform and session_chat_id:
        metadata.setdefault(
            "callback_delivery",
            {
                "platform": session_platform,
                "chat_id": session_chat_id,
                "thread_id": session_thread_id or None,
            },
        )
    return _api_request(
        "POST",
        "/agents/self/delegate",
        {
            "target_agent": target_agent,
            "instruction": instruction,
            "title": args.get("title"),
            "metadata": metadata,
        },
    )


def _check_requirements():
    return bool(
        os.environ.get("HERMESHQ_INTERNAL_API_URL")
        and os.environ.get("HERMESHQ_AGENT_ID")
        and os.environ.get("HERMESHQ_AGENT_TOKEN")
    )


def register(ctx):
    ctx.register_tool(
        name="hq_list_agents",
        toolset="hermeshq_comms",
        schema={
            "name": "hq_list_agents",
            "description": "List the live HermesHQ agent roster, including hierarchy and whether delegated tasks are allowed from your current agent.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=hq_list_agents_tool,
        check_fn=_check_requirements,
        description="List live HermesHQ agents and hierarchy",
        emoji="🛰️",
    )
    ctx.register_tool(
        name="hq_direct_message",
        toolset="hermeshq_comms",
        schema={
            "name": "hq_direct_message",
            "description": "Send a non-task direct message to another HermesHQ agent by id, slug, friendly name, or technical name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_agent": {
                        "type": "string",
                        "description": "Agent id, slug, friendly name, or technical name to contact.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The direct message content to deliver.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Optional metadata payload.",
                    },
                },
                "required": ["target_agent", "message"],
            },
        },
        handler=hq_direct_message_tool,
        check_fn=_check_requirements,
        description="Send a direct inter-agent message inside HermesHQ",
        emoji="📨",
    )
    ctx.register_tool(
        name="hq_delegate_task",
        toolset="hermeshq_comms",
        schema={
            "name": "hq_delegate_task",
            "description": "Delegate executable work to another HermesHQ agent by id, slug, friendly name, or technical name. HermesHQ will enforce hierarchy rules.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_agent": {
                        "type": "string",
                        "description": "Agent id, slug, friendly name, or technical name to delegate to.",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "The task instruction to execute.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional short title for the delegated task.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Optional metadata payload.",
                    },
                },
                "required": ["target_agent", "instruction"],
            },
        },
        handler=hq_delegate_task_tool,
        check_fn=_check_requirements,
        description="Delegate executable work to another HermesHQ agent",
        emoji="🔀",
    )
