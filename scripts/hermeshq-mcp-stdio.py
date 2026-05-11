#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _call_hermeshq(payload: dict) -> dict:
    base_url = os.environ.get("HERMESHQ_URL", "http://localhost:8000").rstrip("/")
    token = os.environ.get("HERMESHQ_MCP_TOKEN", "").strip()
    if not token:
        return _error(payload.get("id"), -32001, "HERMESHQ_MCP_TOKEN is required")
    request = urllib.request.Request(
        f"{base_url}/mcp",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return _error(payload.get("id"), -32002, f"HermesHQ HTTP {exc.code}: {body}")
    except Exception as exc:
        return _error(payload.get("id"), -32003, f"HermesHQ connection failed: {exc}")
    if not body.strip():
        return {"jsonrpc": "2.0", "id": payload.get("id"), "result": {}}
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return _error(payload.get("id"), -32004, f"HermesHQ returned non-JSON response: {body[:500]}")


def main() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(_error(None, -32700, f"Invalid JSON: {exc}"))
            continue
        if payload.get("id") is None and str(payload.get("method") or "").startswith("notifications/"):
            _call_hermeshq(payload)
            continue
        _write(_call_hermeshq(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
