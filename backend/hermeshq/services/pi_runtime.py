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
    ) -> None:
        self.session_factory = session_factory
        self.secret_vault = secret_vault
        self.workspace_manager = workspace_manager
        self.installation_manager = PiInstallationManager(secret_vault, workspace_manager, session_factory)
        self._active: dict[str, tuple[asyncio.subprocess.Process, PiRpcClient]] = {}

    @property
    def available(self) -> bool:
        return shutil.which("node") is not None and PI_RUNNER_SCRIPT.exists()

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

        await self.installation_manager.sync_agent_installation(agent)
        env = self.installation_manager.build_process_env(agent)
        workspace = self.workspace_manager.build_workspace_path(agent.id)

        process = await asyncio.create_subprocess_exec(
            "node", str(PI_RUNNER_SCRIPT),
            cwd=str(workspace),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        client = PiRpcClient(process.stdin, process.stdout)
        self._active[agent.id] = (process, client)

        try:
            config = agent.pi_config or {}
            await client.init({
                "tools": config.get("tools", ["read", "bash", "edit"]),
                "thinking_level": config.get("thinking_level", "medium"),
                "system_prompt": self.installation_manager.compose_system_prompt(agent),
                "model": agent.model or "anthropic/claude-sonnet-4",
            })

            prompt = task.prompt or ""
            if conversation_history:
                history_text = "\n\n".join(
                    f"[{m.get('role', 'unknown')}]: {m.get('content', '')}"
                    for m in conversation_history[-10:]
                )
                if history_text.strip():
                    prompt = f"Previous conversation:\n{history_text}\n\n---\n\n{prompt}"

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

        except asyncio.TimeoutError:
            raise RuntimeExecutionError("Pi agent timed out")
        except Exception as exc:
            if isinstance(exc, RuntimeExecutionError):
                raise
            raise RuntimeExecutionError(f"Pi runtime error: {exc}") from exc
        finally:
            client_task = asyncio.create_task(client.close())
            kill_task = asyncio.create_task(self._terminate(process))
            await asyncio.gather(client_task, kill_task, return_exceptions=True)
            self._active.pop(agent.id, None)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except (ProcessLookupError, asyncio.TimeoutError):
            with __import__("contextlib").suppress(ProcessLookupError):
                process.kill()
