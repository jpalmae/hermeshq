import asyncio
import importlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.secret import Secret
from hermeshq.models.task import Task
from hermeshq.services.hermes_installation import HermesInstallationManager
from hermeshq.services.secret_vault import SecretVault


@dataclass
class RuntimeExecutionResult:
    final_response: str
    messages: list[dict]
    tool_calls: list[dict]
    tokens_used: int
    iterations: int
    engine: str


class RuntimeExecutionError(RuntimeError):
    pass


class HermesRuntime:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_vault: SecretVault,
        installation_manager: HermesInstallationManager,
    ) -> None:
        self.session_factory = session_factory
        self.secret_vault = secret_vault
        self.installation_manager = installation_manager
        self._agent_class = self._load_agent_class()

    @property
    def available(self) -> bool:
        return self._agent_class is not None

    def _load_agent_class(self):
        try:
            module = importlib.import_module("run_agent")
            return getattr(module, "AIAgent", None)
        except Exception:
            return None

    async def execute(
        self,
        agent: Agent,
        task: Task,
        stream_callback,
        conversation_history: list[dict] | None = None,
        session_id: str | None = None,
    ) -> RuntimeExecutionResult:
        if not self._agent_class:
            raise RuntimeExecutionError("hermes-agent runtime is not installed in the backend environment")
        if not self._has_credentials(agent):
            raise RuntimeExecutionError("No runtime credentials configured for this agent")
        api_key = await self._resolve_api_key(agent.api_key_ref)
        await self.installation_manager.sync_agent_installation(agent)
        runtime_system_prompt = await self.installation_manager.get_runtime_system_prompt(agent)
        try:
            return await self._run_real(
                agent,
                task,
                stream_callback,
                api_key,
                runtime_system_prompt,
                conversation_history=conversation_history,
                session_id=session_id,
            )
        except RuntimeExecutionError:
            raise
        except Exception as exc:
            raise RuntimeExecutionError(str(exc)) from exc

    async def _run_real(
        self,
        agent: Agent,
        task: Task,
        stream_callback,
        api_key: str | None,
        runtime_system_prompt: str,
        conversation_history: list[dict] | None = None,
        session_id: str | None = None,
    ) -> RuntimeExecutionResult:
        workspace_path = self.installation_manager.resolve_workspace_path(agent.workspace_path)
        hermes_home = self.installation_manager.build_hermes_home(agent.workspace_path)
        process_env = await self.installation_manager.build_process_env(agent)
        payload = {
            "task_id": str(task.id),
            "prompt": task.prompt,
            "system_override": task.system_override,
            "model": agent.model,
            "provider": agent.provider,
            "base_url": agent.base_url,
            "api_key": api_key,
            "enabled_toolsets": agent.enabled_toolsets or None,
            "disabled_toolsets": agent.disabled_toolsets or None,
            "max_iterations": agent.max_iterations,
            "system_prompt": runtime_system_prompt,
            "cwd": str(workspace_path),
            "hermes_home": str(hermes_home),
            "conversation_history": conversation_history or [],
            "session_id": session_id,
        }

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "hermes_task_runner.py"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
            cwd=str(Path(__file__).resolve().parents[2]),
            env={**process_env, "HERMESHQ_TASK_PAYLOAD": json.dumps(payload)},
        )

        final_result: dict | None = None

        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                await stream_callback(text)
                continue

            if event.get("event") == "delta" and event.get("data"):
                await stream_callback(str(event["data"]))
            elif event.get("event") == "result":
                final_result = event
            elif event.get("event") == "error":
                raise RuntimeExecutionError(str(event.get("error") or "Hermes runtime process failed"))

        stderr_output = ""
        if process.stderr is not None:
            stderr_output = (await process.stderr.read()).decode("utf-8", errors="replace").strip()

        return_code = await process.wait()
        if return_code != 0 and not final_result:
            raise RuntimeExecutionError(stderr_output or "Hermes runtime process exited with an error")
        if not final_result:
            raise RuntimeExecutionError("Hermes runtime returned no result payload")

        return RuntimeExecutionResult(
            final_response=str(final_result.get("final_response") or "").strip(),
            messages=list(final_result.get("messages") or []),
            tool_calls=list(final_result.get("tool_calls") or []),
            tokens_used=int(final_result.get("tokens_used") or 0),
            iterations=int(final_result.get("iterations") or 0),
            engine=str(final_result.get("engine") or "hermes-agent"),
        )

    async def _resolve_api_key(self, api_key_ref: str | None) -> str | None:
        if not api_key_ref:
            return None
        try:
            async with self.session_factory() as session:
                result = await session.execute(select(Secret).where(Secret.name == api_key_ref))
                secret = result.scalar_one_or_none()
            if not secret:
                raise RuntimeExecutionError(f"Secret '{api_key_ref}' was not found")
            return self.secret_vault.decrypt(secret.value_enc)
        except RuntimeExecutionError:
            raise
        except Exception as exc:
            raise RuntimeExecutionError(f"Could not resolve secret '{api_key_ref}'") from exc

    def _has_credentials(self, agent: Agent) -> bool:
        if agent.api_key_ref:
            return True
        return any(
            os.getenv(env_name)
            for env_name in (
                "OPENROUTER_API_KEY",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "KIMI_API_KEY",
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
                "GLM_API_KEY",
                "ZAI_API_KEY",
                "Z_AI_API_KEY",
            )
        )

    def _extract_tool_calls(self, messages: list[dict]) -> list[dict]:
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
