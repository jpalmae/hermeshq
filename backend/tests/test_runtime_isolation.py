from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from hermeshq.config import Settings
from hermeshq.runtime_runner import (
    ExecutionRequest,
    GatewayRequest,
    _isolated_environment,
    _prepare_execution_network,
    _redact_runtime_error,
    _resolve_workspace_volume,
    build_container_command,
    build_gateway_command,
)

AGENT_ID = UUID("11111111-1111-4111-8111-111111111111")
EXECUTION_ID = UUID("22222222-2222-4222-8222-222222222222")


def execution_request(**overrides) -> ExecutionRequest:
    values = {
        "engine": "hermes",
        "execution_id": EXECUTION_ID,
        "agent_id": AGENT_ID,
        "environment": {"OPENAI_API_KEY": "agent-secret"},
        "input_data": "{}",
        "hermes_version": None,
    }
    values.update(overrides)
    return ExecutionRequest(**values)


def test_container_command_enforces_security_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_NETWORK", "hermeshq_agent_runtime")
    monkeypatch.setenv("RUNTIME_IMAGE", "hermeshq-runtime:test")
    request = execution_request()

    runtime_network = f"hq-net-{EXECUTION_ID.hex}"
    command, container_name = build_container_command(
        request,
        "/tmp/runtime.env",
        "project_workspaces",
        runtime_network,
    )
    rendered = " ".join(command)

    assert container_name == f"hq-run-{EXECUTION_ID.hex}"
    assert "--user 1000:1000" in rendered
    assert "--read-only" in command
    assert "--cap-drop ALL" in rendered
    assert "--security-opt no-new-privileges:true" in rendered
    assert "--pids-limit 256" in rendered
    assert f"--network {runtime_network}" in rendered
    assert "hermeshq.runtime-owner=hermeshq-runtime-runner" in command
    assert "--ipc none" in rendered
    assert "--log-driver none" in rendered
    assert "/var/run/docker.sock" not in rendered
    assert "agent-11111111-1111-4111-8111-111111111111" in rendered
    assert "agent-33333333-3333-4333-8333-333333333333" not in rendered
    assert command[-2:] == ["/opt/venv/bin/python", "/app/hermeshq/scripts/hermes_task_runner.py"]


def test_managed_hermes_version_is_the_only_read_only_runtime_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_IMAGE", "hermeshq-runtime:test")
    request = execution_request(hermes_version="0.9.0")

    command, _ = build_container_command(
        request,
        "/tmp/runtime.env",
        "project_workspaces",
        f"hq-net-{EXECUTION_ID.hex}",
    )
    mounts = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]

    assert len(mounts) == 2
    assert mounts[0].endswith(
        "dst=/app/workspaces/agent-11111111-1111-4111-8111-111111111111,"
        "volume-subpath=agent-11111111-1111-4111-8111-111111111111"
    )
    assert mounts[1].endswith(
        "dst=/app/workspaces/_hermes_versions/0.9.0,volume-subpath=_hermes_versions/0.9.0,readonly"
    )
    assert command[-2] == "/app/workspaces/_hermes_versions/0.9.0/.venv/bin/python"


def test_gateway_uses_a_long_lived_hardened_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_IMAGE", "hermeshq-runtime:test")
    request = GatewayRequest(
        agent_id=AGENT_ID,
        environment={"TELEGRAM_BOT_TOKEN": "agent-token"},
        hermes_version="0.9.0",
    )

    command, container_name = build_gateway_command(
        request,
        "/tmp/runtime.env",
        "project_workspaces",
        f"hq-gw-net-{AGENT_ID.hex}",
    )
    rendered = " ".join(command)

    assert container_name == f"hq-gateway-{AGENT_ID.hex}"
    assert "--detach" in command
    assert "--rm" not in command
    assert "--interactive" not in command
    assert "--read-only" in command
    assert "hermeshq.runtime-gateway=true" in command
    assert "--cap-drop ALL" in rendered
    assert f"--network hq-gw-net-{AGENT_ID.hex}" in rendered
    assert command[-2] == "-c"
    assert command[-1].startswith("exec /app/workspaces/_hermes_versions/0.9.0/.venv/bin/hermes gateway run --replace")
    assert "agent-11111111-1111-4111-8111-111111111111/.hermes/logs/gateway.log" in command[-1]


def test_runner_overrides_security_sensitive_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_EGRESS_PROXY_URL", "http://runtime-egress:3128")
    request = execution_request(
        environment={
            "HOME": "/stolen",
            "HERMES_HOME": "/other-agent",
            "HERMESHQ_INTERNAL_API_URL": "http://attacker",
            "HTTPS_PROXY": "http://attacker",
            "OPENAI_API_KEY": "agent-secret",
        }
    )

    environment = _isolated_environment(request)

    workspace = "/app/workspaces/agent-11111111-1111-4111-8111-111111111111"
    assert environment["HOME"] == "/home/appuser"
    assert environment["HERMES_HOME"] == f"{workspace}/.hermes"
    assert environment["PI_CODING_AGENT_DIR"] == f"{workspace}/.pi"
    assert environment["HERMESHQ_INTERNAL_API_URL"] == "http://backend:8000/api/internal"
    assert environment["HTTPS_PROXY"] == "http://runtime-egress:3128"
    assert environment["OPENAI_API_KEY"] == "agent-secret"


@pytest.mark.asyncio
async def test_each_execution_gets_a_private_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def docker_output(*arguments: str) -> str:
        calls.append(arguments)
        return "network-id" if arguments[:2] == ("network", "create") else ""

    monkeypatch.setattr("hermeshq.runtime_runner._docker_output", docker_output)

    network = await _prepare_execution_network(EXECUTION_ID)

    assert network == f"hq-net-{EXECUTION_ID.hex}"
    assert calls[0][:3] == ("network", "create", "--internal")
    assert calls[1] == ("network", "connect", "--alias", "backend", network, "hermeshq-backend")
    assert calls[2] == (
        "network",
        "connect",
        "--alias",
        "runtime-egress",
        network,
        "hermeshq-runtime-egress",
    )


@pytest.mark.asyncio
async def test_runner_discovers_an_unlabelled_workspace_volume_from_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNTIME_WORKSPACES_VOLUME", raising=False)

    async def docker_output(*arguments: str) -> str:
        assert arguments == (
            "inspect",
            "--format",
            "{{json .Mounts}}",
            "hermeshq-backend",
        )
        return json.dumps(
            [
                {
                    "Type": "volume",
                    "Name": "legacy_hermeshq_workspaces",
                    "Destination": "/app/workspaces",
                }
            ]
        )

    monkeypatch.setattr("hermeshq.runtime_runner._docker_output", docker_output)

    assert await _resolve_workspace_volume() == "legacy_hermeshq_workspaces"


def test_runtime_errors_redact_agent_secrets() -> None:
    request = execution_request(environment={"OPENAI_API_KEY": "super-secret-agent-key"})

    redacted = _redact_runtime_error("provider rejected super-secret-agent-key", request)

    assert redacted == "provider rejected [REDACTED]"


@pytest.mark.parametrize(
    ("environment", "version"),
    [
        ({"SAFE": "first\nINJECTED=second"}, None),
        ({"BAD-NAME": "value"}, None),
        ({}, "../../host"),
    ],
)
def test_runner_rejects_env_file_and_version_injection(environment: dict[str, str], version: str | None) -> None:
    with pytest.raises(ValidationError):
        execution_request(environment=environment, hermes_version=version)


def test_production_requires_isolated_runtime() -> None:
    with pytest.raises(RuntimeError, match="RUNTIME_ISOLATION_MODE=required"):
        Settings(
            _env_file=None,
            debug=False,
            jwt_secret="strong-jwt-secret-that-is-long-enough",
            fernet_key="independent-fernet-seed",
            admin_password="strong-admin-password",
            runtime_isolation_mode="subprocess",
        )


def test_required_isolation_requires_a_strong_runner_token() -> None:
    with pytest.raises(RuntimeError, match="RUNTIME_RUNNER_TOKEN"):
        Settings(
            _env_file=None,
            debug=False,
            jwt_secret="strong-jwt-secret-that-is-long-enough",
            fernet_key="independent-fernet-seed",
            admin_password="strong-admin-password",
            runtime_isolation_mode="required",
            runtime_runner_token="short",
        )


def test_egress_proxy_denies_private_targets_before_allowing_domains() -> None:
    config = Path(__file__).resolve().parents[1] / "runtime-egress" / "squid.conf"
    text = config.read_text(encoding="utf-8")

    assert text.index("http_access deny local_targets") < text.index("http_access allow allowed_domains")
    assert text.rstrip().endswith("request_header_access X-Forwarded-For deny all")


def test_pi_runner_does_not_shadow_static_imports() -> None:
    runner = Path(__file__).resolve().parents[1] / "hermeshq" / "scripts" / "pi_runner.mjs"
    text = runner.read_text(encoding="utf-8")

    assert 'await import("path")' not in text
    assert 'await import("fs")' not in text
