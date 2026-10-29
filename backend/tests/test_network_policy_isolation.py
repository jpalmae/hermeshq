from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from hermeshq.runtime_runner import (
    ExecutionRequest,
    NetworkPolicy,
    _isolated_environment,
    _prepare_execution_network,
    build_egress_container_command,
)
from hermeshq.services.permission_enforcer import _NETWORK_COMMAND_RE, enforceable_egress_domains
from hermeshq.services.runtime_runner_client import RuntimeRunnerClient

AGENT_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_enforceable_egress_domains_translates_policy_patterns() -> None:
    assert enforceable_egress_domains(["*.openai.com", "graph.microsoft.com"]) == [
        ".openai.com",
        "graph.microsoft.com",
    ]


def test_enforceable_egress_domains_drops_unenforceable_patterns() -> None:
    assert enforceable_egress_domains(["*", "api.*.com", "", None, 42, "example.com."]) == ["example.com"]
    assert enforceable_egress_domains("not-a-list") == []
    assert enforceable_egress_domains(None) == []


EXECUTION_ID = UUID("22222222-2222-4222-8222-222222222222")


def execution_request(**overrides) -> ExecutionRequest:
    values = {
        "engine": "hermes",
        "execution_id": EXECUTION_ID,
        "agent_id": AGENT_ID,
        "environment": {},
        "input_data": "{}",
        "hermes_version": None,
    }
    values.update(overrides)
    return ExecutionRequest(**values)


def test_deny_all_without_domains_removes_the_egress_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_EGRESS_PROXY_URL", "http://runtime-egress:3128")
    request = execution_request(network_policy={"deny_all": True, "allow_domains": []})

    environment = _isolated_environment(request)

    assert "HTTP_PROXY" not in environment
    assert "HTTPS_PROXY" not in environment
    assert "NO_PROXY" not in environment


def test_deny_all_with_allowed_domains_keeps_the_proxy_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_EGRESS_PROXY_URL", "http://runtime-egress:3128")
    request = execution_request(
        network_policy={"deny_all": True, "allow_domains": [".api.openai.com"]},
    )

    environment = _isolated_environment(request)

    assert environment["HTTP_PROXY"] == "http://runtime-egress:3128"
    assert environment["HTTPS_PROXY"] == "http://runtime-egress:3128"


def test_policy_without_deny_all_keeps_default_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_EGRESS_PROXY_URL", "http://runtime-egress:3128")
    request = execution_request(network_policy={"deny_all": False, "allow_domains": []})

    environment = _isolated_environment(request)

    assert environment["HTTP_PROXY"] == "http://runtime-egress:3128"


@pytest.mark.asyncio
async def test_isolated_execution_network_skips_the_shared_egress_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def docker_output(*arguments: str) -> str:
        calls.append(arguments)
        return "network-id" if arguments[:2] == ("network", "create") else ""

    monkeypatch.setattr("hermeshq.runtime_runner._docker_output", docker_output)

    network = await _prepare_execution_network(EXECUTION_ID, include_egress=False)

    assert network == f"hq-net-{EXECUTION_ID.hex}"
    connect_calls = [item for item in calls if item[:2] == ("network", "connect")]
    assert ("network", "connect", "--alias", "backend", network, "hermeshq-backend") in connect_calls
    assert not any("runtime-egress" in item for item in connect_calls)


def test_per_execution_egress_container_is_hardened_and_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_EGRESS_IMAGE", "hermeshq-runtime-egress:local")

    command = build_egress_container_command(
        f"hq-egress-{EXECUTION_ID.hex}",
        f"hq-net-{EXECUTION_ID.hex}",
        [".api.openai.com", "example.com"],
    )
    rendered = " ".join(command)

    assert f"--name hq-egress-{EXECUTION_ID.hex}" in rendered
    assert f"--network hq-net-{EXECUTION_ID.hex}" in rendered
    assert "--network-alias runtime-egress" in rendered
    assert "--cap-drop ALL" in rendered
    assert "--security-opt no-new-privileges:true" in rendered
    assert "--read-only" in command
    assert "RUNTIME_EGRESS_ALLOWLIST=.api.openai.com,example.com" in rendered
    assert command[-1] == "hermeshq-runtime-egress:local"


def test_network_policy_rejects_malformed_domains() -> None:
    with pytest.raises(ValidationError):
        NetworkPolicy(deny_all=True, allow_domains=["bad domain"])

    with pytest.raises(ValidationError):
        NetworkPolicy(deny_all=True, allow_domains=["http://example.com"])


@pytest.mark.asyncio
async def test_runtime_client_sends_the_network_policy_to_the_runner() -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        async def aiter_lines(self):
            yield json.dumps({"_runner": "exit", "exit_code": 0})

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Client:
        def stream(self, method, path, json=None, headers=None):
            captured["payload"] = json
            return Stream()

        async def delete(self, path, **kwargs) -> None:
            pass

    client = RuntimeRunnerClient.__new__(RuntimeRunnerClient)
    client._base_url = "http://runtime-runner:8080"
    client._token = "a" * 32
    client._client = Client()

    policy = {"deny_all": True, "allow_domains": [".api.openai.com"]}

    async def consume() -> None:
        async for _ in client.run(
            engine="pi",
            agent_id=str(AGENT_ID),
            environment={},
            input_data="",
            network_policy=policy,
        ):
            pass

    await consume()

    assert captured["payload"]["network_policy"] == policy


@pytest.mark.asyncio
async def test_runtime_client_omits_the_network_policy_when_unset() -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        async def aiter_lines(self):
            yield json.dumps({"_runner": "exit", "exit_code": 0})

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Client:
        def stream(self, method, path, json=None, headers=None):
            captured["payload"] = json
            return Stream()

        async def delete(self, path, **kwargs) -> None:
            pass

    client = RuntimeRunnerClient.__new__(RuntimeRunnerClient)
    client._base_url = "http://runtime-runner:8080"
    client._token = "a" * 32
    client._client = Client()

    async def consume() -> None:
        async for _ in client.run(engine="pi", agent_id=str(AGENT_ID), environment={}, input_data=""):
            pass

    await consume()

    assert "network_policy" not in captured["payload"]


@pytest.mark.parametrize(
    "command",
    [
        "curl http://example.com",
        "/usr/bin/curl http://example.com",
        "echo $(curl http://example.com)",
        "busybox wget http://example.com",
        "/usr/bin/git clone http://example.com/repo.git",
        "pip install requests",
        "echo ok && curl http://example.com",
    ],
)
def test_network_command_filter_closes_known_bypasses(command: str) -> None:
    assert _NETWORK_COMMAND_RE.search(command), command


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "echo hello world",
        "python3 script.py",
        "git status",
        "cat README.md",
    ],
)
def test_network_command_filter_does_not_flag_ordinary_commands(command: str) -> None:
    assert not _NETWORK_COMMAND_RE.search(command), command
