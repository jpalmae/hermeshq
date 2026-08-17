from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.task import Task
from hermeshq.services.pi_installation import PiInstallationManager
from hermeshq.services.pi_rpc_client import PiRpcClient
from hermeshq.services.runtime_base import RuntimeBase, RuntimeExecutionError, RuntimeExecutionResult
from hermeshq.services.runtime_runner_client import RuntimeRunnerClient
from hermeshq.services.secret_vault import SecretVault

logger = logging.getLogger(__name__)

PI_RUNNER_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pi_runner.mjs"


class PiRuntime(RuntimeBase):
    """Executes Pi agents as Node.js subprocesses via RPC."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_vault: SecretVault,
        workspace_manager,
        runtime_runner_client: RuntimeRunnerClient | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.secret_vault = secret_vault
        self.workspace_manager = workspace_manager
        self.installation_manager = PiInstallationManager(secret_vault, workspace_manager, session_factory)
        self.runtime_runner_client = runtime_runner_client
        self._active: set[str] = set()

    @property
    def available(self) -> bool:
        return self.runtime_runner_client is not None or (
            shutil.which("node") is not None and PI_RUNNER_SCRIPT.exists()
        )

    @staticmethod
    def _task_timeout_seconds() -> int:
        from hermeshq.config import get_settings

        return max(60, int(get_settings().task_timeout_seconds))

    async def execute(
        self,
        agent: Agent,
        task: Task,
        stream_callback=None,
        conversation_history: list[dict] | None = None,
        session_id: str | None = None,
    ) -> RuntimeExecutionResult:
        if not self.available:
            raise RuntimeExecutionError("Pi runtime is not available (node or runner script missing)")

        if agent.id in self._active:
            raise RuntimeExecutionError(f"Agent {agent.id} already has an active Pi session")

        await self.installation_manager.sync_agent_installation(agent)
        env = await self.installation_manager.build_process_env(agent)
        workspace = self.workspace_manager.build_workspace_path(agent.id)
        config = agent.pi_config or {}
        prompt = task.prompt or ""
        if conversation_history:
            history_text = "\n\n".join(
                f"[{message.get('role', 'unknown')}]: {message.get('content', '')}"
                for message in conversation_history[-10:]
            )
            if history_text.strip():
                prompt = f"Previous conversation:\n{history_text}\n\n---\n\n{prompt}"

        self._active.add(agent.id)
        try:
            if self.runtime_runner_client is not None:
                return await self._execute_isolated(agent, config, prompt, env, stream_callback)
            return await self._execute_subprocess(agent, config, prompt, env, workspace, stream_callback)
        finally:
            self._active.discard(agent.id)

    async def _execute_isolated(
        self,
        agent: Agent,
        config: dict,
        prompt: str,
        env: dict[str, str],
        stream_callback,
    ) -> RuntimeExecutionResult:
        assert self.runtime_runner_client is not None
        input_data = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "init",
                        "params": {
                            "tools": config.get("tools", ["read", "bash", "edit"]),
                            "thinking_level": config.get("thinking_level", "medium"),
                            "system_prompt": self.installation_manager.compose_system_prompt(agent),
                            "model": agent.model or "anthropic/claude-sonnet-4",
                        },
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "prompt", "params": {"text": prompt}}),
                "",
            ]
        )
        try:
            async with asyncio.timeout(self._task_timeout_seconds()):
                async for line in self.runtime_runner_client.run(
                    engine="pi",
                    agent_id=agent.id,
                    environment=env,
                    input_data=input_data,
                ):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Pi isolated runtime returned invalid JSON")
                        continue
                    if event.get("type") == "text_delta" and stream_callback:
                        await stream_callback(str(event.get("delta") or ""))
                    elif event.get("type") == "done":
                        return RuntimeExecutionResult(
                            final_response=str(event.get("response") or ""),
                            messages=list(event.get("messages") or []),
                            tool_calls=list(event.get("tool_calls") or []),
                            tokens_used=int(event.get("tokens") or 0),
                            iterations=int(event.get("turns") or 1),
                            engine="pi",
                            response_attachments=list(event.get("attachments") or []),
                        )
                    elif event.get("type") == "error":
                        raise RuntimeExecutionError(f"Pi execution error: {event.get('error')}")
        except TimeoutError as exc:
            raise RuntimeExecutionError("Pi agent timed out") from exc
        raise RuntimeExecutionError("Pi agent ended without producing a result")

    async def _execute_subprocess(
        self,
        agent: Agent,
        config: dict,
        prompt: str,
        env: dict[str, str],
        workspace: Path,
        stream_callback,
    ) -> RuntimeExecutionResult:

        process = await asyncio.create_subprocess_exec(
            "node",
            str(PI_RUNNER_SCRIPT),
            cwd=str(workspace),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        client = PiRpcClient(process.stdin, process.stdout)

        try:
            await client.init(
                {
                    "tools": config.get("tools", ["read", "bash", "edit"]),
                    "thinking_level": config.get("thinking_level", "medium"),
                    "system_prompt": self.installation_manager.compose_system_prompt(agent),
                    "model": agent.model or "anthropic/claude-sonnet-4",
                }
            )

            async for event in client.prompt(prompt):
                if event["type"] == "text_delta" and stream_callback:
                    await stream_callback(event["delta"])
                elif event["type"] == "done":
                    return RuntimeExecutionResult(
                        final_response=event["response"],
                        messages=event["messages"],
                        tool_calls=event["tool_calls"],
                        tokens_used=event["tokens"],
                        iterations=event["turns"],
                        engine="pi",
                        response_attachments=event["attachments"],
                    )

            raise RuntimeExecutionError("Pi agent ended without producing a result")

        except TimeoutError:
            raise RuntimeExecutionError("Pi agent timed out")
        except Exception as exc:
            if isinstance(exc, RuntimeExecutionError):
                raise
            raise RuntimeExecutionError(f"Pi runtime error: {exc}") from exc
        finally:
            client_task = asyncio.create_task(client.close())
            kill_task = asyncio.create_task(self._terminate(process))
            await asyncio.gather(client_task, kill_task, return_exceptions=True)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except (TimeoutError, ProcessLookupError):
            with __import__("contextlib").suppress(ProcessLookupError):
                process.kill()
