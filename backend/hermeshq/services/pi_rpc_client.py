from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)


class PiRpcClient:
    """JSON-RPC client over stdin/stdout for communicating with a Pi subprocess."""

    def __init__(self, stdin: asyncio.StreamWriter, stdout: asyncio.StreamReader) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._msg_id = 0
        self._read_task: asyncio.Task | None = None
        self._event_queue: asyncio.Queue[dict] = asyncio.Queue()

    @staticmethod
    def _task_timeout_seconds() -> int:
        from hermeshq.config import get_settings

        return max(60, int(get_settings().task_timeout_seconds))

    async def _send(self, method: str, params: dict | None = None) -> None:
        self._msg_id += 1
        msg = {"jsonrpc": "2.0", "id": self._msg_id, "method": method}
        if params:
            msg["params"] = params
        data = (json.dumps(msg) + "\n").encode()
        self._stdin.write(data)
        await self._stdin.drain()

    async def _read_loop(self) -> None:
        while True:
            line = await self._stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                logger.warning("Pi RPC: invalid JSON line: %s", line[:200])
                continue
            await self._event_queue.put(msg)

    async def init(self, config: dict) -> None:
        self._read_task = asyncio.create_task(self._read_loop())
        await self._send("init", config)

        # Wait for init confirmation (with timeout)
        try:
            while True:
                msg = await asyncio.wait_for(self._event_queue.get(), timeout=30.0)
                if msg.get("type") == "ready":
                    break
                if msg.get("type") == "error":
                    raise RuntimeError(f"Pi init failed: {msg.get('error', 'unknown')}")
        except asyncio.TimeoutError:
            raise RuntimeError("Pi init timed out after 30s") from None

    async def prompt(self, text: str) -> dict:
        """Send a prompt and return the final result."""
        await self._send("prompt", {"text": text})

        messages: list[dict] = []
        tool_calls: list[dict] = []
        response_parts: list[str] = []
        timeout = self._task_timeout_seconds()

        while True:
            try:
                msg = await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(f"Pi prompt timed out after {timeout}s") from None

            if msg.get("type") == "text_delta":
                response_parts.append(msg.get("delta", ""))
                yield {"type": "text_delta", "delta": msg["delta"]}

            elif msg.get("type") == "tool_call":
                tool_calls.append({"tool": msg["tool"], "input": msg.get("input", {})})
                yield {"type": "tool_call", "tool": msg["tool"], "input": msg.get("input", {})}

            elif msg.get("type") == "done":
                yield {
                    "type": "done",
                    "response": msg.get("response", "".join(response_parts)),
                    "messages": msg.get("messages", messages),
                    "tool_calls": msg.get("tool_calls", tool_calls),
                    "tokens": msg.get("tokens", 0),
                    "turns": msg.get("turns", 1),
                    "attachments": msg.get("attachments", []),
                }
                break

            elif msg.get("type") == "error":
                raise RuntimeError(f"Pi execution error: {msg.get('error')}")

    async def abort(self) -> None:
        await self._send("abort")

    async def close(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError):
                await self._read_task
