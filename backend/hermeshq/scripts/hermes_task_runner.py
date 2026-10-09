from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _extract_tool_calls(messages: list[dict]) -> list[dict]:
    extracted: list[dict] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls", []) or []:
            extracted.append(
                {
                    "name": tool_call.get("function", {}).get("name", "tool"),
                    "status": "completed",
                    "payload": tool_call,
                }
            )
    return extracted


def main() -> int:
    # Payload arrives via stdin (keeps the API key out of the process
    # environment, where it would be visible via /proc/<pid>/environ).
    # Fall back to the legacy env var for backwards compatibility.
    payload_raw = ""
    if not sys.stdin.isatty():
        try:
            payload_raw = sys.stdin.read().strip()
        except OSError:
            payload_raw = ""
    if not payload_raw:
        payload_raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    if not payload_raw:
        _emit({"event": "error", "error": "Missing task payload (stdin or HERMESHQ_TASK_PAYLOAD)"})
        return 1

    payload = json.loads(payload_raw)
    os.environ["HERMES_HOME"] = payload["hermes_home"]
    os.environ["HERMES_QUIET"] = "1"
    os.environ.setdefault("TERM", "xterm-256color")

    os.chdir(payload["cwd"])

    # ── Plugin discovery ────────────────────────────────────────
    # One-shot task subprocesses construct AIAgent directly and never enter a
    # hermes_cli entrypoint, so user plugins (including hermeshq_guard) are not
    # discovered and their hooks never register. Force discovery against the
    # agent home so policy enforcement and telemetry run in tasks too.
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered(force=True)
    except Exception as _plugin_err:  # noqa: BLE001  # plugins are best-effort in tasks
        _emit({"event": "warning", "warning": f"plugin discovery failed: {_plugin_err}"})

    # ── Auxiliary client env injection ─────────────────────────
    # When HermesHQ passes explicit api_key + base_url, the main agent
    # client is constructed directly. But the auxiliary clients (vision,
    # compression, etc.) resolve credentials independently via env vars.
    # Inject the agent's credentials so auxiliary tasks can use them too.
    _api_key = payload.get("api_key")
    _base_url = payload.get("base_url")
    if _api_key and _base_url:
        os.environ.setdefault("OPENAI_API_KEY", _api_key)
        os.environ.setdefault("OPENAI_BASE_URL", _base_url)
        for _task in ("vision", "compression", "session_search", "web_extract"):
            os.environ.setdefault(f"AUXILIARY_{_task.upper()}_API_KEY", _api_key)
            os.environ.setdefault(f"AUXILIARY_{_task.upper()}_BASE_URL", _base_url)
    # ── End auxiliary client env injection ─────────────────────

    # ── Attachment enrichment ──────────────────────────────────
    task_metadata = payload.get("metadata", {})
    attachments = task_metadata.get("attachments", [])
    if attachments:
        attachment_lines = []
        for att in attachments:
            att_path = att.get("path", "")
            if att_path:
                full_path = Path(payload["cwd"]) / att_path
                if full_path.exists():
                    line = f"- [{att.get('media_type', 'file')}] {att_path}"
                    if att.get("filename"):
                        line += f" (filename: {att['filename']})"
                    if att.get("caption"):
                        line += f" — {att['caption']}"
                    attachment_lines.append(line)
        if attachment_lines:
            payload["prompt"] += "\n\nAttached files:\n" + "\n".join(attachment_lines)
    # ── End attachment enrichment ──────────────────────────────

    try:
        from run_agent import AIAgent

        emitted_chunks: list[str] = []

        def on_delta(delta: str) -> None:
            emitted_chunks.append(delta)
            _emit({"event": "delta", "data": delta})

        agent = AIAgent(
            model=payload["model"],
            provider=payload.get("provider"),
            base_url=payload.get("base_url"),
            api_key=payload.get("api_key"),
            session_id=payload.get("session_id"),
            quiet_mode=True,
            enabled_toolsets=payload.get("enabled_toolsets") or None,
            disabled_toolsets=payload.get("disabled_toolsets") or None,
            ephemeral_system_prompt=payload.get("system_prompt"),
            max_iterations=payload.get("max_iterations", 90),
            skip_context_files=False,
            skip_memory=False,
            platform="hermeshq",
            stream_delta_callback=on_delta,
        )

        result = agent.run_conversation(
            user_message=payload["prompt"],
            task_id=payload["task_id"],
            system_message=payload.get("system_override"),
            conversation_history=payload.get("conversation_history") or None,
        )

        messages = result.get("messages", [])
        final_response = (result.get("final_response") or "".join(emitted_chunks)).strip()
        tool_calls = _extract_tool_calls(messages)
        assistant_messages = [message for message in messages if message.get("role") == "assistant"]
        if not final_response and not assistant_messages and not tool_calls:
            raise RuntimeError("Hermes runtime returned no assistant output")

        _emit(
            {
                "event": "result",
                "final_response": final_response,
                "messages": messages,
                "tool_calls": tool_calls,
                "tokens_used": max(256, len(str(result).split())),
                "iterations": len(assistant_messages),
                "engine": "hermes-agent",
            }
        )
        return 0
    except Exception as exc:  # noqa: BLE001  # CLI top-level catch-all for error reporting
        _emit(
            {
                "event": "error",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc(),
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
