from __future__ import annotations

import json
import threading
import time
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx

from hermeshq.services.runtime_base import RuntimeExecutionError


class RuntimeRunnerClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=10, read=None, write=30, pool=30),
        )

    @staticmethod
    def workspace_path(agent_id: str) -> str:
        return f"/app/workspaces/agent-{agent_id}"

    async def run(
        self,
        *,
        engine: str,
        agent_id: str,
        environment: dict[str, str],
        input_data: str,
        hermes_version: str | None = None,
    ) -> AsyncIterator[str]:
        payload = {
            "engine": engine,
            "execution_id": str(uuid4()),
            "agent_id": agent_id,
            "environment": environment,
            "input_data": input_data,
            "hermes_version": hermes_version,
        }
        exit_event: dict | None = None
        try:
            async with self._client.stream(
                "POST",
                "/v1/executions",
                json=payload,
                headers={"X-Runtime-Runner-Token": self._token},
            ) as response:
                if response.status_code != 200:
                    raise RuntimeExecutionError(
                        f"Isolated runtime runner rejected the execution ({response.status_code})"
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        yield line
                        continue
                    if event.get("_runner") == "exit":
                        exit_event = event
                        continue
                    yield line
        except httpx.HTTPError as exc:
            raise RuntimeExecutionError("Isolated runtime runner is unavailable") from exc

        if exit_event is None:
            raise RuntimeExecutionError("Isolated runtime runner ended without an exit status")
        if int(exit_event.get("exit_code", 1)) != 0:
            detail = str(exit_event.get("error") or "Isolated runtime container failed")
            raise RuntimeExecutionError(detail)

    async def health(self) -> bool:
        try:
            response = await self._client.get(
                "/health",
                headers={"X-Runtime-Runner-Token": self._token},
            )
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def start_gateway(
        self,
        *,
        agent_id: str,
        environment: dict[str, str],
        hermes_version: str | None,
    ) -> RemoteGatewayProcess:
        try:
            response = await self._client.post(
                "/v1/gateways",
                json={
                    "agent_id": agent_id,
                    "environment": environment,
                    "hermes_version": hermes_version,
                },
                headers={"X-Runtime-Runner-Token": self._token},
            )
        except httpx.HTTPError as exc:
            raise RuntimeExecutionError("Isolated runtime runner is unavailable") from exc
        if response.status_code != 200:
            raise RuntimeExecutionError(f"Isolated runtime runner rejected the gateway ({response.status_code})")
        payload = response.json()
        return RemoteGatewayProcess(
            self._base_url,
            self._token,
            agent_id,
            int(payload["pid"]),
        )

    async def close(self) -> None:
        await self._client.aclose()


class RemoteGatewayProcess:
    def __init__(self, base_url: str, token: str, agent_id: str, pid: int) -> None:
        self.pid = pid
        self._agent_id = agent_id
        self._headers = {"X-Runtime-Runner-Token": token}
        self._client = httpx.Client(base_url=base_url, timeout=5)
        self._lock = threading.Lock()
        self._return_code: int | None = None

    def poll(self) -> int | None:
        with self._lock:
            if self._return_code is not None:
                return self._return_code
            try:
                response = self._client.get(f"/v1/gateways/{self._agent_id}", headers=self._headers)
            except httpx.HTTPError:
                return None
            if response.status_code == 404:
                self._return_code = 1
                return self._return_code
            if response.status_code != 200:
                return None
            payload = response.json()
            if bool(payload.get("running")):
                return None
            self._return_code = int(payload.get("exit_code") or 0)
            return self._return_code

    def wait(self) -> int:
        while True:
            return_code = self.poll()
            if return_code is not None:
                self._delete()
                self._client.close()
                return return_code
            time.sleep(1)

    def terminate(self) -> None:
        with self._lock:
            self._delete()
            self._return_code = 0

    def kill(self) -> None:
        self.terminate()

    def _delete(self) -> None:
        try:
            self._client.delete(f"/v1/gateways/{self._agent_id}", headers=self._headers)
        except httpx.HTTPError:
            pass
