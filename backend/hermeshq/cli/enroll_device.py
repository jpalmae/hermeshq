#!/usr/bin/env python3
"""Enroll a local machine as an execution device for a HermesHQ agent.

Runs on the END USER's machine — stdlib only, no hermeshq package needed.

Usage:
    python enroll_device.py enroll https://hq.example.com --agent-id <uuid> --name "MacBook"
    python enroll_device.py sync
    python enroll_device.py run                # sync+heartbeat loop (default 300s)
    python enroll_device.py status
    python enroll_device.py desktop            # print how to launch Hermes Desktop

State lives in ~/.hermes-hq/enrollment.json. The agent's HERMES_HOME defaults
to ~/.hermes-hq/agents/<agent-id> and is populated by `sync` with config,
guard plugin and credentials pulled from the HermesHQ bundle.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path.home() / ".hermes-hq"
STATE_FILE = STATE_DIR / "enrollment.json"
PROVIDER_ENV_FALLBACK = {
    "nvidia": ["NVIDIA_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "zai": ["ZAI_API_KEY", "GLM_API_KEY"],
    "gemini": ["GEMINI_API_KEY"],
    "kimi-coding": ["KIMI_API_KEY"],
}


class CliError(RuntimeError):
    pass


def _request(method: str, url: str, *, token: str | None = None, payload: dict | None = None, timeout: float = 30.0):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
            parsed: dict = json.loads(body) if body else {}
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise CliError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CliError(f"Cannot reach {url}: {exc.reason}") from exc


def _load_state() -> dict:
    if not STATE_FILE.exists():
        raise CliError(f"No enrollment found at {STATE_FILE}. Run `enroll` first.")
    state: dict = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return state


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.chmod(STATE_FILE, 0o600)


def _yaml_str(value: str) -> str:
    value = str(value)
    if value == "":
        return "''"
    needs_quote = any(ch in value for ch in ":#{}[]&*!|>'\"%@`") or value.startswith((" ", "-")) or value.endswith(" ")
    if needs_quote:
        return json.dumps(value)
    return value


def _dump_yaml(data: dict, indent: int = 0) -> str:
    lines = []
    pad = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            lines.append(_dump_yaml(value, indent + 1))
        elif isinstance(value, list):
            lines.append(f"{pad}{key}:")
            for item in value:
                lines.append(f"{pad}  - {_yaml_str(item)}")
        elif value is None:
            lines.append(f"{pad}{key}: null")
        else:
            lines.append(f"{pad}{key}: {_yaml_str(value)}")
    return "\n".join(lines)


def _login(server: str) -> str:
    username = input("HermesHQ username: ").strip()
    password = getpass.getpass("Password: ")
    _, body = _request(
        "POST",
        f"{server}/api/auth/login",
        payload={"username": username, "password": password},
    )
    token = body.get("access_token")
    if not token:
        raise CliError("Login did not return a token")
    return str(token)


def cmd_enroll(args: argparse.Namespace) -> None:
    server = args.server.rstrip("/")
    user_token = _login(server)
    _, enroll_response = _request(
        "POST",
        f"{server}/api/enrollment/enroll",
        token=user_token,
        payload={
            "agent_id": args.agent_id,
            "device_name": args.name,
            "os_info": {"platform": sys.platform, "hostname": os.uname().nodename},
            "guard_fail_mode": args.fail_mode,
        },
    )
    enroll_token = enroll_response.pop("enroll_token", None)
    device_id = enroll_response.get("id")
    if not enroll_token or not device_id:
        raise CliError("Enrollment response missing token or device id")
    activation_request = urllib.request.Request(
        f"{server}/api/enrollment/devices/{device_id}/activate",
        data=json.dumps({"os_info": {}}).encode(),
        headers={"Content-Type": "application/json", "X-HermesHQ-Enroll-Token": enroll_token},
        method="POST",
    )
    with urllib.request.urlopen(activation_request, timeout=30) as response:
        activation_body = json.loads(response.read().decode())
    device_token = activation_body.get("device_token")
    if not device_token:
        raise CliError("Activation did not return a device token")

    home = Path(args.home).expanduser() if args.home else STATE_DIR / "agents" / args.agent_id
    state = {
        "server": server,
        "device_id": device_id,
        "device_token": device_token,
        "agent_id": args.agent_id,
        "agent_name": enroll_response.get("name", ""),
        "hermes_home": str(home),
        "etag": None,
        "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_state(state)
    print(f"Enrolled. Device {device_id} → agent {args.agent_id}")
    print(f"HERMES_HOME: {home}")
    print("Run `sync` now to pull the agent bundle.")


def cmd_activate(args: argparse.Namespace) -> None:
    server = args.server.rstrip("/")
    request = urllib.request.Request(
        f"{server}/api/enrollment/devices/{args.device_id}/activate",
        data=json.dumps({"os_info": {"platform": sys.platform, "hostname": os.uname().nodename}}).encode(),
        headers={"Content-Type": "application/json", "X-HermesHQ-Enroll-Token": args.token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise CliError(f"Activation failed: HTTP {exc.code} {exc.read().decode()[:200]}") from exc
    device = body.get("device", {})
    device_token = body.get("device_token")
    if not device_token:
        raise CliError("Activation did not return a device token")
    home = Path(args.home).expanduser() if args.home else STATE_DIR / "agents" / device.get("agent_id", "unknown")
    state = {
        "server": server,
        "device_id": args.device_id,
        "device_token": device_token,
        "agent_id": device.get("agent_id", ""),
        "agent_name": device.get("name", ""),
        "hermes_home": str(home),
        "etag": None,
        "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_state(state)
    print(f"Activated device {args.device_id} → agent {state['agent_id']}")
    print(f"HERMES_HOME: {home}")
    print("Run `sync` now to pull the agent bundle.")


def _write_home(state: dict, bundle: dict) -> None:
    home = Path(state["hermes_home"])
    home.mkdir(parents=True, exist_ok=True)
    agent = bundle["agent"]
    guard = bundle["guard"]

    guard_env = guard.get("env", {})
    env_lines = [f"{key}={value}" for key, value in guard_env.items()]
    env_lines.append(f"HERMESHQ_GUARD_FAIL_MODE={guard.get('fail_mode', 'fail-open')}")
    api_key = agent.get("api_key")
    if api_key:
        for env_name in PROVIDER_ENV_FALLBACK.get((agent.get("provider") or "").lower(), ["OPENAI_API_KEY"]):
            env_lines.append(f"{env_name}={api_key}")
    if agent.get("base_url"):
        env_lines.append(f"OPENAI_BASE_URL={agent['base_url']}")
    env_path = home / ".env"
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)

    config = {
        "model": {
            "default": agent.get("model"),
            "provider": agent.get("provider"),
            "base_url": agent.get("base_url"),
        },
        "agent": {"system_prompt": agent.get("system_prompt") or ""},
        "skills": {"external_dirs": []},
        "plugins": {"enabled": ["hermeshq_guard"]},
    }
    (home / "config.yaml").write_text(_dump_yaml(config) + "\n", encoding="utf-8")
    os.chmod(home / "config.yaml", 0o600)

    if agent.get("soul_md"):
        (home / "SOUL.md").write_text(agent["soul_md"], encoding="utf-8")

    plugin_root = home / "plugins" / "hermeshq_guard"
    plugin_root.mkdir(parents=True, exist_ok=True)
    for rel_path, content in (guard.get("plugin_files") or {}).items():
        target = plugin_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    print(f"Synced bundle → {home}")


def cmd_sync(args: argparse.Namespace) -> None:
    state = _load_state()
    headers = {"X-HermesHQ-Device-Token": state["device_token"]}
    url = f"{state['server']}/api/enrollment/devices/bundle"
    if state.get("etag") and not args.force:
        url += f"?since={state['etag']}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status == 304:
                print("Bundle unchanged (304).")
                _heartbeat(state, None)
                return
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise CliError("Device token rejected — device may be revoked. Re-enroll.") from exc
        raise CliError(f"Bundle fetch failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CliError(f"Cannot reach server: {exc.reason}") from exc

    _write_home(state, body["bundle"])
    state["etag"] = body["etag"]
    _save_state(state)
    _heartbeat(state, body["etag"])
    print(f"Sync complete (etag {body['etag']}).")


def _heartbeat(state: dict, etag: str | None) -> None:
    try:
        request = urllib.request.Request(
            f"{state['server']}/api/enrollment/devices/heartbeat",
            data=json.dumps({"bundle_etag": etag}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-HermesHQ-Device-Token": state["device_token"],
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
    except Exception as exc:  # noqa: BLE001  # heartbeat is best-effort
        print(f"heartbeat failed: {exc}", file=sys.stderr)


def cmd_run(args: argparse.Namespace) -> None:
    _load_state()
    interval = args.interval
    print(f"Running enroll agent: sync every {interval}s (Ctrl+C to stop)")
    while True:
        try:
            cmd_sync(args=argparse.Namespace(force=False))
        except CliError as exc:
            print(f"sync error: {exc}", file=sys.stderr)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("Stopping.")
            return


def cmd_status(_args: argparse.Namespace) -> None:
    state = _load_state()
    print(json.dumps({k: v for k, v in state.items() if k != "device_token"}, indent=2))
    try:
        request = urllib.request.Request(
            f"{state['server']}/api/enrollment/devices/heartbeat",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-HermesHQ-Device-Token": state["device_token"],
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode())
            print("server:", json.dumps(body, indent=2))
    except Exception as exc:  # noqa: BLE001  # status is best-effort
        print(f"server unreachable: {exc}", file=sys.stderr)


def cmd_desktop(_args: argparse.Namespace) -> None:
    state = _load_state()
    print("Launch Hermes Desktop against the enrolled home with:")
    print(f'  HERMES_HOME="{state["hermes_home"]}" hermes desktop')
    print(f"  (shell: export HERMES_HOME={state['hermes_home']}; hermes desktop)")


def main() -> int:
    parser = argparse.ArgumentParser(description="HermesHQ device enrollment")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enroll_parser = subparsers.add_parser("enroll", help="Enroll this machine for an agent")
    enroll_parser.add_argument("server", help="HermesHQ base URL, e.g. https://hq.example.com")
    enroll_parser.add_argument("--agent-id", required=True)
    enroll_parser.add_argument("--name", default=os.uname().nodename, help="Device name shown in HQ")
    enroll_parser.add_argument(
        "--home", default=None, help="HERMES_HOME for the agent (default ~/.hermes-hq/agents/<id>)"
    )
    enroll_parser.add_argument(
        "--fail-mode", default="fail-open", help="fail-open | fail-closed | fail-grace:<seconds>"
    )
    enroll_parser.set_defaults(func=cmd_enroll)

    activate_parser = subparsers.add_parser("activate", help="Activate a pending device with an enrollment token")
    activate_parser.add_argument("server", help="HermesHQ base URL")
    activate_parser.add_argument("--device-id", required=True)
    activate_parser.add_argument("--token", required=True, help="Enrollment token from the admin UI")
    activate_parser.add_argument("--home", default=None, help="HERMES_HOME for the agent")
    activate_parser.set_defaults(func=cmd_activate)

    sync_parser = subparsers.add_parser("sync", help="Pull the latest agent bundle")
    sync_parser.add_argument("--force", action="store_true", help="Ignore etag and re-download")
    sync_parser.set_defaults(func=cmd_sync)

    run_parser = subparsers.add_parser("run", help="Sync + heartbeat loop")
    run_parser.add_argument("--interval", type=int, default=300)
    run_parser.add_argument("--force", action="store_true")
    run_parser.set_defaults(func=cmd_run)

    status_parser = subparsers.add_parser("status", help="Show enrollment state")
    status_parser.set_defaults(func=cmd_status)

    desktop_parser = subparsers.add_parser("desktop", help="Show how to launch Desktop on the enrolled home")
    desktop_parser.set_defaults(func=cmd_desktop)

    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
