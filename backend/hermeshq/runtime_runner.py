from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_RESOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,31}$")
_RESERVED_ENV = {
    "HOME",
    "HERMES_HOME",
    "HERMESHQ_INTERNAL_API_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "PI_AGENT_DIR",
    "PI_CODING_AGENT_DIR",
    "PATH",
}


class RuntimeEnvironmentRequest(BaseModel):
    agent_id: UUID
    environment: dict[str, str] = Field(default_factory=dict)
    hermes_version: str | None = None

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 512:
            raise ValueError("Too many environment variables")
        total_size = 0
        for key, item in value.items():
            if not _ENV_KEY_RE.fullmatch(key):
                raise ValueError("Invalid environment variable name")
            if "\n" in item or "\r" in item or "\x00" in item:
                raise ValueError("Environment variable values must be single-line text")
            total_size += len(key) + len(item)
        if total_size > 256 * 1024:
            raise ValueError("Environment is too large")
        return value

    @field_validator("hermes_version")
    @classmethod
    def validate_version(cls, value: str | None) -> str | None:
        if value is not None and not _VERSION_RE.fullmatch(value):
            raise ValueError("Invalid Hermes version")
        return value


class ExecutionRequest(RuntimeEnvironmentRequest):
    engine: Literal["hermes", "pi"]
    execution_id: UUID
    input_data: str = Field(max_length=8 * 1024 * 1024)


class GatewayRequest(RuntimeEnvironmentRequest):
    pass


def _required_resource_name(env_key: str, default: str) -> str:
    value = os.environ.get(env_key, default)
    if not _RESOURCE_NAME_RE.fullmatch(value):
        raise RuntimeError(f"{env_key} is invalid")
    return value


def _positive_int(env_key: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(env_key, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{env_key} must be an integer") from exc
    if value < 1 or value > maximum:
        raise RuntimeError(f"{env_key} is outside the allowed range")
    return value


def _positive_float(env_key: str, default: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(env_key, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{env_key} must be a number") from exc
    if value <= 0 or value > maximum:
        raise RuntimeError(f"{env_key} is outside the allowed range")
    return value


def _memory_limit() -> str:
    value = os.environ.get("RUNTIME_CONTAINER_MEMORY", "768m").lower()
    if not re.fullmatch(r"[1-9][0-9]{0,5}[kmg]", value):
        raise RuntimeError("RUNTIME_CONTAINER_MEMORY is invalid")
    return value


def _isolated_environment(request: RuntimeEnvironmentRequest) -> dict[str, str]:
    agent_id = str(request.agent_id)
    workspace = f"/app/workspaces/agent-{agent_id}"
    env = {key: value for key, value in request.environment.items() if key not in _RESERVED_ENV}
    proxy_url = os.environ.get("RUNTIME_EGRESS_PROXY_URL", "http://runtime-egress:3128").strip()
    env.update(
        {
            "HOME": "/home/appuser",
            "HERMES_HOME": f"{workspace}/.hermes",
            "HERMESHQ_INTERNAL_API_URL": os.environ.get(
                "RUNTIME_INTERNAL_API_URL", "http://backend:8000/api/internal"
            ).rstrip("/"),
            "PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
            "PI_AGENT_DIR": "/run/hermeshq/pi",
            "PI_CODING_AGENT_DIR": "/run/hermeshq/pi",
        }
    )
    if proxy_url:
        env["HTTP_PROXY"] = proxy_url
        env["HTTPS_PROXY"] = proxy_url
        env["NO_PROXY"] = "backend,localhost,127.0.0.1"
    return env


def build_container_command(
    request: ExecutionRequest,
    env_file: str,
    workspace_volume: str | None = None,
    runtime_network: str | None = None,
) -> tuple[list[str], str]:
    workspace_volume = workspace_volume or _required_resource_name("RUNTIME_WORKSPACES_VOLUME", "hermeshq_workspaces")
    if not _RESOURCE_NAME_RE.fullmatch(workspace_volume):
        raise RuntimeError("Workspace volume name is invalid")
    runtime_network = runtime_network or _required_resource_name("RUNTIME_NETWORK", "hermeshq_agent_runtime")
    if not _RESOURCE_NAME_RE.fullmatch(runtime_network):
        raise RuntimeError("Runtime network name is invalid")
    runtime_image = os.environ.get("RUNTIME_IMAGE", "hermeshq-runtime:local")
    if not runtime_image or runtime_image.startswith("-") or any(char.isspace() for char in runtime_image):
        raise RuntimeError("RUNTIME_IMAGE is invalid")

    agent_id = str(request.agent_id)
    workspace_subpath = f"agent-{agent_id}"
    workspace = f"/app/workspaces/{workspace_subpath}"
    container_name = f"hq-run-{request.execution_id.hex}"
    command = [
        shutil.which("docker") or "/usr/local/bin/docker",
        "run",
        "--rm",
        "--interactive",
        "--init",
        "--name",
        container_name,
        "--hostname",
        f"agent-{agent_id[:12]}",
        "--label",
        "hermeshq.runtime-execution=true",
        "--label",
        "hermeshq.runtime-owner=hermeshq-runtime-runner",
        "--user",
        "1000:1000",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        str(_positive_int("RUNTIME_CONTAINER_PIDS", 256, 4096)),
        "--memory",
        _memory_limit(),
        "--cpus",
        str(_positive_float("RUNTIME_CONTAINER_CPUS", 1.0, 32.0)),
        "--ulimit",
        "nofile=1024:1024",
        "--ulimit",
        "core=0",
        "--ipc",
        "none",
        "--network",
        runtime_network,
        "--log-driver",
        "none",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m,uid=1000,gid=1000,mode=1770",
        "--tmpfs",
        "/home/appuser:rw,noexec,nosuid,nodev,size=64m,uid=1000,gid=1000,mode=0700",
        "--env-file",
        env_file,
        "--mount",
        f"type=volume,src={workspace_volume},dst={workspace},volume-subpath={workspace_subpath}",
        "--workdir",
        workspace,
    ]
    if request.engine == "hermes" and request.hermes_version:
        version_subpath = f"_hermes_versions/{request.hermes_version}"
        version_target = f"/app/workspaces/{version_subpath}"
        command.extend(
            [
                "--mount",
                f"type=volume,src={workspace_volume},dst={version_target},volume-subpath={version_subpath},readonly",
            ]
        )
    elif request.engine == "pi":
        pi_config_subpath = f"_runtime_config/pi/agent-{agent_id}"
        command.extend(
            [
                "--mount",
                f"type=volume,src={workspace_volume},dst=/run/hermeshq/pi,volume-subpath={pi_config_subpath},readonly",
            ]
        )

    command.append(runtime_image)
    if request.engine == "hermes":
        python_bin = (
            f"/app/workspaces/_hermes_versions/{request.hermes_version}/.venv/bin/python"
            if request.hermes_version
            else "/opt/venv/bin/python"
        )
        command.extend([python_bin, "/app/hermeshq/scripts/hermes_task_runner.py"])
    else:
        command.extend(["/usr/bin/node", "/app/hermeshq/scripts/pi_runner.mjs"])
    return command, container_name


def build_gateway_command(
    request: GatewayRequest,
    env_file: str,
    workspace_volume: str,
    runtime_network: str,
) -> tuple[list[str], str]:
    execution_request = ExecutionRequest(
        engine="hermes",
        execution_id=request.agent_id,
        agent_id=request.agent_id,
        environment=request.environment,
        input_data="",
        hermes_version=request.hermes_version,
    )
    command, _ = build_container_command(
        execution_request,
        env_file,
        workspace_volume,
        runtime_network,
    )
    command.remove("--rm")
    command.remove("--interactive")
    command.insert(2, "--detach")
    container_name = f"hq-gateway-{request.agent_id.hex}"
    command[command.index("--name") + 1] = container_name
    runtime_image_index = command.index(os.environ.get("RUNTIME_IMAGE", "hermeshq-runtime:local"))
    command[runtime_image_index:runtime_image_index] = ["--label", "hermeshq.runtime-gateway=true"]
    runtime_image_index += 2
    del command[runtime_image_index + 1 :]
    workspace = f"/app/workspaces/agent-{request.agent_id}"
    hermes_bin = (
        f"/app/workspaces/_hermes_versions/{request.hermes_version}/.venv/bin/hermes"
        if request.hermes_version
        else "/opt/venv/bin/hermes"
    )
    gateway_command = f"exec {hermes_bin} gateway run --replace >> {workspace}/.hermes/logs/gateway.log 2>&1"
    command.extend(["/bin/sh", "-c", gateway_command])
    return command, container_name


async def _read_stderr(stream: asyncio.StreamReader) -> str:
    captured = bytearray()
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if len(captured) < 16 * 1024:
            captured.extend(chunk[: 16 * 1024 - len(captured)])
    return captured.decode("utf-8", errors="replace").strip()


async def _docker_output(*arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        shutil.which("docker") or "/usr/local/bin/docker",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    if process.returncode != 0:
        raise RuntimeError("Docker metadata lookup failed")
    return stdout.decode("utf-8", errors="replace").strip()


async def _resolve_workspace_volume() -> str:
    configured = os.environ.get("RUNTIME_WORKSPACES_VOLUME", "").strip()
    if configured:
        if not _RESOURCE_NAME_RE.fullmatch(configured):
            raise RuntimeError("RUNTIME_WORKSPACES_VOLUME is invalid")
        return configured

    backend_container = _required_resource_name("RUNTIME_BACKEND_CONTAINER", "hermeshq-backend")
    with contextlib.suppress(RuntimeError, TimeoutError, json.JSONDecodeError):
        mounts = json.loads(await _docker_output("inspect", "--format", "{{json .Mounts}}", backend_container))
        candidates = [
            mount["Name"]
            for mount in mounts
            if isinstance(mount, dict)
            and mount.get("Type") == "volume"
            and mount.get("Destination") == "/app/workspaces"
            and isinstance(mount.get("Name"), str)
        ]
        if len(candidates) == 1 and _RESOURCE_NAME_RE.fullmatch(candidates[0]):
            return candidates[0]

    project = ""
    container_id = os.environ.get("HOSTNAME", "").strip()
    if container_id:
        with contextlib.suppress(RuntimeError, TimeoutError):
            project = await _docker_output(
                "inspect",
                "--format",
                '{{ index .Config.Labels "com.docker.compose.project" }}',
                container_id,
            )
    arguments = ["volume", "ls", "--quiet", "--filter", "label=com.docker.compose.volume=hermeshq_workspaces"]
    if project:
        arguments.extend(["--filter", f"label=com.docker.compose.project={project}"])
    candidates = [item for item in (await _docker_output(*arguments)).splitlines() if item]
    if len(candidates) != 1 or not _RESOURCE_NAME_RE.fullmatch(candidates[0]):
        raise RuntimeError("Could not uniquely resolve the HermesHQ workspaces volume")
    return candidates[0]


async def _prepare_execution_network(execution_id: UUID) -> str:
    network_name = f"hq-net-{execution_id.hex}"
    await _prepare_private_network(network_name)
    return network_name


async def _prepare_private_network(network_name: str) -> None:
    if not _RESOURCE_NAME_RE.fullmatch(network_name):
        raise RuntimeError("Runtime network name is invalid")
    backend_container = _required_resource_name("RUNTIME_BACKEND_CONTAINER", "hermeshq-backend")
    egress_container = _required_resource_name("RUNTIME_EGRESS_CONTAINER", "hermeshq-runtime-egress")
    await _docker_output(
        "network",
        "create",
        "--internal",
        "--label",
        "hermeshq.runtime-execution=true",
        "--label",
        "hermeshq.runtime-owner=hermeshq-runtime-runner",
        network_name,
    )
    try:
        await _docker_output("network", "connect", "--alias", "backend", network_name, backend_container)
        await _docker_output("network", "connect", "--alias", "runtime-egress", network_name, egress_container)
    except (RuntimeError, TimeoutError):
        await _teardown_execution_network(network_name)
        raise


async def _teardown_execution_network(network_name: str) -> None:
    for env_key, default in (
        ("RUNTIME_BACKEND_CONTAINER", "hermeshq-backend"),
        ("RUNTIME_EGRESS_CONTAINER", "hermeshq-runtime-egress"),
    ):
        container = _required_resource_name(env_key, default)
        with contextlib.suppress(RuntimeError, TimeoutError):
            await _docker_output("network", "disconnect", "--force", network_name, container)
    with contextlib.suppress(RuntimeError, TimeoutError):
        await _docker_output("network", "rm", network_name)


async def _cleanup_stale_executions() -> None:
    label_filter = "label=hermeshq.runtime-owner=hermeshq-runtime-runner"
    containers = [
        item for item in (await _docker_output("ps", "--all", "--quiet", "--filter", label_filter)).splitlines() if item
    ]
    for container in containers:
        with contextlib.suppress(RuntimeError, TimeoutError):
            await _docker_output("rm", "--force", container)
    networks = [
        item
        for item in (await _docker_output("network", "ls", "--quiet", "--filter", label_filter)).splitlines()
        if item
    ]
    for network in networks:
        await _teardown_execution_network(network)


def _redact_runtime_error(error: str, request: RuntimeEnvironmentRequest) -> str:
    redacted = error
    for value in request.environment.values():
        if len(value) >= 8:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted[: 16 * 1024]


def _write_environment_file(request: RuntimeEnvironmentRequest) -> str:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="hermeshq-runtime-",
        dir="/tmp",
        delete=False,
    ) as env_file:
        os.chmod(env_file.name, 0o600)
        for key, value in sorted(_isolated_environment(request).items()):
            env_file.write(f"{key}={value}\n")
        return env_file.name


async def _remove_container(container_name: str) -> None:
    process = await asyncio.create_subprocess_exec(
        shutil.which("docker") or "/usr/local/bin/docker",
        "rm",
        "--force",
        container_name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), timeout=10)


async def _execute_container(request: ExecutionRequest) -> AsyncIterator[bytes]:
    env_path = ""
    process: asyncio.subprocess.Process | None = None
    container_name = ""
    execution_network = ""
    timed_out = False
    try:
        env_path = _write_environment_file(request)

        workspace_volume = await _resolve_workspace_volume()
        execution_network = await _prepare_execution_network(request.execution_id)
        command, container_name = build_container_command(
            request,
            env_path,
            workspace_volume,
            execution_network,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(_read_stderr(process.stderr))
        process.stdin.write(request.input_data.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        deadline = asyncio.get_running_loop().time() + _positive_int("RUNTIME_MAX_SECONDS", 3600, 86400)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            except TimeoutError:
                timed_out = True
                break
            if not line:
                break
            yield line if line.endswith(b"\n") else line + b"\n"

        if timed_out and process.returncode is None:
            process.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=10)
        stderr = await stderr_task
        exit_code = 124 if timed_out else int(process.returncode or 0)
        error = "Isolated runtime exceeded its execution timeout" if timed_out else stderr
        yield (
            json.dumps(
                {
                    "_runner": "exit",
                    "exit_code": exit_code,
                    "error": _redact_runtime_error(error, request),
                }
            )
            + "\n"
        ).encode()
    except Exception as exc:
        yield (
            json.dumps(
                {
                    "_runner": "exit",
                    "exit_code": 125,
                    "error": _redact_runtime_error(f"Runtime isolation setup failed: {exc}", request),
                }
            )
            + "\n"
        ).encode()
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=10)
        if container_name:
            await _remove_container(container_name)
        if execution_network:
            await _teardown_execution_network(execution_network)
        if env_path:
            with contextlib.suppress(OSError):
                Path(env_path).unlink()


def _gateway_resource_names(agent_id: UUID) -> tuple[str, str]:
    return f"hq-gateway-{agent_id.hex}", f"hq-gw-net-{agent_id.hex}"


def _execution_resource_names(execution_id: UUID) -> tuple[str, str]:
    return f"hq-run-{execution_id.hex}", f"hq-net-{execution_id.hex}"


async def _stop_gateway_container(agent_id: UUID) -> None:
    container_name, network_name = _gateway_resource_names(agent_id)
    await _remove_container(container_name)
    await _teardown_execution_network(network_name)


async def _start_gateway_container(request: GatewayRequest) -> dict[str, str | int]:
    env_path = ""
    container_name, network_name = _gateway_resource_names(request.agent_id)
    await _stop_gateway_container(request.agent_id)
    try:
        workspace_volume = await _resolve_workspace_volume()
        await _prepare_private_network(network_name)
        env_path = _write_environment_file(request)
        command, container_name = build_gateway_command(
            request,
            env_path,
            workspace_volume,
            network_name,
        )
        container_id = await _docker_output(*command[1:])
        return {
            "container_id": container_id,
            "container_name": container_name,
            "pid": int(container_id[:8], 16),
        }
    except Exception:
        await _stop_gateway_container(request.agent_id)
        raise
    finally:
        if env_path:
            with contextlib.suppress(OSError):
                Path(env_path).unlink()


async def _gateway_container_status(agent_id: UUID) -> dict[str, bool | int]:
    container_name, _ = _gateway_resource_names(agent_id)
    try:
        raw_state = await _docker_output("inspect", "--format", "{{json .State}}", container_name)
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=404, detail="Gateway container not found") from exc
    state = json.loads(raw_state)
    return {
        "running": bool(state.get("Running")),
        "exit_code": int(state.get("ExitCode") or 0),
    }


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await _cleanup_stale_executions()
    yield


app = FastAPI(
    title="HermesHQ Runtime Runner",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)
_semaphore = asyncio.Semaphore(_positive_int("RUNTIME_RUNNER_MAX_CONCURRENCY", 8, 128))


def _authorize(token: str | None) -> None:
    expected = os.environ.get("RUNTIME_RUNNER_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", expected):
        raise HTTPException(status_code=503, detail="Runtime runner token is not configured")
    if token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health(x_runtime_runner_token: Annotated[str | None, Header()] = None) -> dict[str, str]:
    _authorize(x_runtime_runner_token)
    docker_path = shutil.which("docker") or "/usr/local/bin/docker"
    if not Path(docker_path).exists() or not Path("/var/run/docker.sock").exists():
        raise HTTPException(status_code=503, detail="Docker is unavailable")
    return {"status": "healthy"}


@app.post("/v1/executions")
async def execute(
    request: ExecutionRequest,
    x_runtime_runner_token: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    _authorize(x_runtime_runner_token)

    async def stream() -> AsyncIterator[bytes]:
        async with _semaphore:
            async for line in _execute_container(request):
                yield line

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/v1/gateways")
async def start_gateway(
    request: GatewayRequest,
    x_runtime_runner_token: Annotated[str | None, Header()] = None,
) -> dict[str, str | int]:
    _authorize(x_runtime_runner_token)
    try:
        async with _semaphore:
            return await _start_gateway_container(request)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Gateway isolation setup failed") from exc


@app.delete("/v1/executions/{execution_id}")
async def stop_execution(
    execution_id: UUID,
    x_runtime_runner_token: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    _authorize(x_runtime_runner_token)
    container_name, network_name = _execution_resource_names(execution_id)
    await _remove_container(container_name)
    await _teardown_execution_network(network_name)
    return {"status": "stopped"}


@app.get("/v1/gateways/{agent_id}")
async def gateway_status(
    agent_id: UUID,
    x_runtime_runner_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool | int]:
    _authorize(x_runtime_runner_token)
    return await _gateway_container_status(agent_id)


@app.delete("/v1/gateways/{agent_id}")
async def stop_gateway(
    agent_id: UUID,
    x_runtime_runner_token: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    _authorize(x_runtime_runner_token)
    await _stop_gateway_container(agent_id)
    return {"status": "stopped"}
