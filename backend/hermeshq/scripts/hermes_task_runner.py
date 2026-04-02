import json
import os
import sys
import traceback


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
    payload_raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    if not payload_raw:
        _emit({"event": "error", "error": "Missing HERMESHQ_TASK_PAYLOAD"})
        return 1

    payload = json.loads(payload_raw)
    os.environ["HERMES_HOME"] = payload["hermes_home"]
    os.environ["HERMES_QUIET"] = "1"
    os.environ.setdefault("TERM", "xterm-256color")

    os.chdir(payload["cwd"])

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
    except Exception as exc:
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
