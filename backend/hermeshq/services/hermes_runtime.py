import asyncio
import json
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.task import Task
from hermeshq.services.credentials import require_secret_value
from hermeshq.services.hermes_installation import HermesInstallationManager
from hermeshq.services.provider_catalog import normalize_runtime_provider
from hermeshq.services.runtime_base import RuntimeBase, RuntimeExecutionError, RuntimeExecutionResult
from hermeshq.services.runtime_profiles import resolve_effective_toolsets
from hermeshq.services.runtime_runner_client import RuntimeRunnerClient
from hermeshq.services.secret_vault import SecretVault

logger = logging.getLogger(__name__)


# ── Response attachment extension/MIME maps (shared with task_runner) ──
_ALLOWED_MEDIA_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".mp3",
    ".aac",
    ".ogg",
    ".wav",
    ".m4a",
    ".flac",
    ".mp4",
    ".webm",
    ".mov",
    ".avi",
    ".mkv",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".csv",
    ".json",
    ".md",
    ".xml",
    ".html",
    ".zip",
}
_MEDIA_EXT_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
    ".xml": "application/xml",
    ".html": "text/html",
    ".zip": "application/zip",
}


class HermesRuntime(RuntimeBase):
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_vault: SecretVault,
        installation_manager: HermesInstallationManager,
        runtime_runner_client: RuntimeRunnerClient | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.secret_vault = secret_vault
        self.installation_manager = installation_manager
        self.runtime_runner_client = runtime_runner_client

    @property
    def available(self) -> bool:
        return True

    @staticmethod
    def _task_timeout_seconds() -> int:
        from hermeshq.config import get_settings

        return max(60, int(get_settings().task_timeout_seconds))

    async def execute(
        self,
        agent: Agent,
        task: Task,
        stream_callback,
        conversation_history: list[dict] | None = None,
        session_id: str | None = None,
    ) -> RuntimeExecutionResult:
        if not self._has_credentials(agent):
            raise RuntimeExecutionError("No runtime credentials configured for this agent")
        api_key = await self._resolve_api_key(agent.api_key_ref)
        await self.installation_manager.sync_agent_installation(agent)
        runtime_system_prompt = await self.installation_manager.get_runtime_system_prompt(agent)
        runtime_selection = await self.installation_manager.resolve_hermes_runtime(agent)
        try:
            return await self._run_real(
                agent,
                task,
                stream_callback,
                api_key,
                runtime_system_prompt,
                runtime_selection.python_bin,
                runtime_selection.effective_version if runtime_selection.source == "managed" else None,
                conversation_history=conversation_history,
                session_id=session_id,
            )
        except RuntimeExecutionError as primary_exc:
            # ── Fallback provider retry ────────────────────────────────
            if not self._has_fallback(agent):
                raise
            try:
                fallback_api_key = await self._resolve_api_key(agent.fallback_api_key_ref)
                fallback_provider_normalized = normalize_runtime_provider(agent.fallback_provider)
                return await self._run_real(
                    agent,
                    task,
                    stream_callback,
                    fallback_api_key,
                    runtime_system_prompt,
                    runtime_selection.python_bin,
                    runtime_selection.effective_version if runtime_selection.source == "managed" else None,
                    conversation_history=conversation_history,
                    session_id=session_id,
                    fallback_override={
                        "model": agent.fallback_model,
                        "provider": fallback_provider_normalized,
                        "base_url": agent.fallback_base_url,
                        "api_key": fallback_api_key,
                    },
                )
            except Exception as fallback_exc:  # noqa: BLE001  # fallback failed — chain both errors
                raise RuntimeExecutionError(f"Primary and fallback providers failed: {fallback_exc}") from primary_exc
        except Exception as exc:  # noqa: BLE001  # runtime execution catch-all → RuntimeExecutionError
            raise RuntimeExecutionError(str(exc)) from exc

    async def _run_real(
        self,
        agent: Agent,
        task: Task,
        stream_callback,
        api_key: str | None,
        runtime_system_prompt: str,
        runtime_python_bin: str,
        runtime_version: str | None,
        conversation_history: list[dict] | None = None,
        session_id: str | None = None,
        fallback_override: dict | None = None,
    ) -> RuntimeExecutionResult:
        workspace_path = self.installation_manager.resolve_workspace_path(agent.workspace_path)
        process_env = await self.installation_manager.build_process_env(agent)
        if fallback_override:
            process_env = {**process_env, **self._fallback_env(agent, fallback_override.get("api_key"))}
        enabled_toolsets, disabled_toolsets = resolve_effective_toolsets(
            agent.runtime_profile,
            agent.enabled_toolsets,
            agent.disabled_toolsets,
        )
        runtime_provider = self.installation_manager._model_provider_for_agent(agent)
        effective_base_url = self.installation_manager._effective_provider_base_url(agent)
        effective_model = await self._resolve_effective_model(agent, runtime_provider)
        # ── Channel routing: suppress external channels for mobile_app tasks ──
        _task_meta = task.metadata_json or {}
        _reply_to = str(_task_meta.get("reply_to") or _task_meta.get("source") or "").strip().lower()
        if _reply_to == "mobile_app":
            runtime_system_prompt = (
                runtime_system_prompt + "\n\n" + "IMPORTANT: You are responding through the SixAgentic mobile app. "
                "Always provide your response directly in the task response text. "
                "Do NOT send responses through Telegram, WhatsApp, email, or any other "
                "external channel. "
                "When you generate files (PDFs, images, spreadsheets, etc.), save them "
                "in your work/ directory — NOT in /tmp or other system locations. "
                "After generating a file, add a line at the END of your response in the "
                "exact format: MEDIA:/path/to/your/file.ext — this is required for the "
                "file to be delivered to the user. "
                "Do NOT mention file paths elsewhere in your response text. "
                "Any files you generate will be automatically collected and attached "
                "as response_attachments."
            )

        execution_workspace = (
            self.runtime_runner_client.workspace_path(agent.id)
            if self.runtime_runner_client is not None
            else str(workspace_path)
        )
        payload = {
            "task_id": str(task.id),
            "prompt": task.prompt,
            "system_override": task.system_override,
            "model": effective_model,
            "provider": runtime_provider,
            "base_url": effective_base_url,
            "api_key": api_key,
            "enabled_toolsets": enabled_toolsets or None,
            "disabled_toolsets": disabled_toolsets or None,
            "max_iterations": agent.max_iterations,
            "system_prompt": runtime_system_prompt,
            "cwd": execution_workspace,
            "hermes_home": f"{execution_workspace}/.hermes",
            "conversation_history": conversation_history or [],
            "session_id": session_id,
            "metadata": task.metadata_json or {},
        }

        if fallback_override:
            for key, value in fallback_override.items():
                if value is not None:
                    payload[key] = value

        process: asyncio.subprocess.Process | None = None
        if self.runtime_runner_client is None:
            process = await asyncio.create_subprocess_exec(
                runtime_python_bin,
                str(Path(__file__).resolve().parents[1] / "scripts" / "hermes_task_runner.py"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
                cwd=str(Path(__file__).resolve().parents[2]),
                env=process_env,
            )

        final_result: dict | None = None

        async def _consume() -> dict | None:
            result: dict | None = None

            async def handle_line(text: str) -> None:
                nonlocal result
                if not text:
                    return
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    await stream_callback(text)
                    return

                if event.get("event") == "delta" and event.get("data"):
                    await stream_callback(str(event["data"]))
                elif event.get("event") == "result":
                    result = event
                elif event.get("event") == "error":
                    raise RuntimeExecutionError(str(event.get("error") or "Hermes runtime process failed"))

            if self.runtime_runner_client is not None:
                async for text in self.runtime_runner_client.run(
                    engine="hermes",
                    agent_id=agent.id,
                    environment=process_env,
                    input_data=json.dumps(payload),
                    hermes_version=runtime_version,
                ):
                    await handle_line(text.strip())
                return result

            assert process is not None
            assert process.stdin is not None
            try:
                process.stdin.write(json.dumps(payload).encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                await handle_line(line.decode("utf-8", errors="replace").strip())
            return result

        try:
            final_result = await asyncio.wait_for(_consume(), timeout=self._task_timeout_seconds())
        except TimeoutError as exc:
            raise RuntimeExecutionError(
                f"Hermes runtime exceeded the task timeout of {self._task_timeout_seconds()}s"
            ) from exc
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    logger.warning("Task runner subprocess %s did not exit after kill", process.pid)

        if process is not None:
            stderr_output = ""
            if process.stderr is not None:
                stderr_output = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
            return_code = process.returncode if process.returncode is not None else await process.wait()
            if return_code != 0 and not final_result:
                raise RuntimeExecutionError(stderr_output or "Hermes runtime process exited with an error")
        if not final_result:
            raise RuntimeExecutionError("Hermes runtime returned no result payload")

        raw_response = str(final_result.get("final_response") or "").strip()

        # ── Detect provider-level errors disguised as successful responses ──
        # hermes-agent catches API errors (429, timeout, auth) and returns
        # them as normal text responses instead of raising errors.
        _PROVIDER_ERROR_PATTERNS = (
            "API call failed",
            "rate limit",
            "Rate limit",
            "429",
            "401",
            "403",
            "Authentication",
            "timeout",
            "Connection",
            "service unavailable",
            "internal server error",
            "insufficient_quota",
            "insufficient quota",
            "quota exceeded",
            "credits",
            "billing",
            "overloaded",
            "capacity",
        )
        if any(p.lower() in raw_response.lower() for p in _PROVIDER_ERROR_PATTERNS) and len(raw_response) < 300:
            raise RuntimeExecutionError(raw_response)

        # ── Parse MEDIA:/FILE: references and collect response attachments ──
        # The agent may emit lines like "MEDIA:/path/to/file.pdf" to indicate
        # files it generated. We extract these, copy to uploads/, and strip
        # the references from the response text.
        import re as _re
        import shutil as _shutil
        import uuid as _uuid
        from pathlib import Path as _Path

        media_attachments: list[dict] = []
        media_pattern = _re.compile(r"(?im)^MEDIA:\s*(.+)$")

        media_matches = list(media_pattern.finditer(raw_response))
        if media_matches:
            uploads_dir = workspace_path / "uploads"
            uploads_dir.mkdir(parents=True, exist_ok=True)
            workspace_root = workspace_path.resolve()
            isolated_root = _Path(execution_workspace)

            for match in media_matches:
                file_path_str = match.group(1).strip()
                file_path = _Path(file_path_str)
                if self.runtime_runner_client is not None and file_path.is_absolute():
                    try:
                        file_path = workspace_path / file_path.relative_to(isolated_root)
                    except ValueError:
                        logger.warning("MEDIA: path is outside the isolated workspace: %s", file_path_str)
                        continue
                if not file_path.exists():
                    file_path = workspace_path / file_path_str
                resolved_file = file_path.resolve()
                if workspace_root not in [resolved_file, *resolved_file.parents]:
                    logger.warning("MEDIA: path escapes the agent workspace: %s", file_path_str)
                    continue
                file_path = resolved_file
                if not file_path.exists() or not file_path.is_file():
                    logger.warning("MEDIA: file not found: %s", file_path_str)
                    continue

                ext = file_path.suffix.lower()
                if ext not in _ALLOWED_MEDIA_EXTS:
                    logger.warning("MEDIA: file extension not allowed: %s", ext)
                    continue

                file_id = str(_uuid.uuid4())
                dest_filename = f"{file_id}{ext}"
                dest_path = uploads_dir / dest_filename
                try:
                    _shutil.copy2(file_path, dest_path)
                except OSError as exc:
                    logger.warning("MEDIA: failed to copy %s: %s", file_path, exc)
                    continue

                file_size = dest_path.stat().st_size
                media_attachments.append(
                    {
                        "file_id": file_id,
                        "filename": file_path.name,
                        "media_type": _MEDIA_EXT_MIME_MAP.get(ext, "application/octet-stream"),
                        "size": file_size,
                        "caption": "",
                        "source_path": str(file_path),
                    }
                )
                logger.info("MEDIA: collected %s (%d bytes) as %s", file_path.name, file_size, file_id)

            # Strip MEDIA: lines from the response text
            raw_response = media_pattern.sub("", raw_response).strip()
            # Also strip any remaining inline MEDIA: references
            raw_response = _re.sub(r"(?i)MEDIA:\s*/\S+", "", raw_response).strip()
            # Clean up any double blank lines left behind
            raw_response = _re.sub(r"\n{3,}", "\n\n", raw_response)

        # Merge with any attachments the task runner already collected
        runner_attachments = list(final_result.get("response_attachments") or [])
        all_attachments = runner_attachments + media_attachments

        return RuntimeExecutionResult(
            final_response=raw_response,
            messages=list(final_result.get("messages") or []),
            tool_calls=list(final_result.get("tool_calls") or []),
            tokens_used=int(final_result.get("tokens_used") or 0),
            iterations=int(final_result.get("iterations") or 0),
            engine=str(final_result.get("engine") or "hermes-agent"),
            response_attachments=all_attachments,
        )

    async def _resolve_api_key(self, api_key_ref: str | None) -> str | None:
        if not api_key_ref:
            return None
        try:
            async with self.session_factory() as session:
                return await require_secret_value(session, self.secret_vault, api_key_ref, RuntimeExecutionError)
        except RuntimeExecutionError:
            raise
        except (KeyError, ValueError, RuntimeError) as exc:
            raise RuntimeExecutionError(f"Could not resolve secret '{api_key_ref}'") from exc

    def _has_fallback(self, agent: Agent) -> bool:
        return bool(agent.fallback_provider or agent.fallback_model or agent.fallback_api_key_ref)

    def _fallback_env(self, agent: Agent, fallback_api_key: str | None) -> dict[str, str]:
        """Build extra env vars for the fallback provider."""
        env: dict[str, str] = {}
        fallback_provider = normalize_runtime_provider(agent.fallback_provider)
        if not fallback_provider:
            return env
        for env_name in self.installation_manager._provider_env_names(fallback_provider):
            if fallback_api_key:
                env[env_name] = fallback_api_key
        if agent.fallback_base_url:
            base_env = self.installation_manager._provider_base_url_env_name(fallback_provider)
            if base_env:
                env[base_env] = agent.fallback_base_url
        # Override auxiliary models to use fallback credentials too,
        # so vision/compression/web_extract don't hit the failing primary.
        if fallback_api_key and agent.fallback_base_url:
            for _aux_task in ("vision", "compression", "web_extract"):
                env[f"AUXILIARY_{_aux_task.upper()}_API_KEY"] = fallback_api_key
                env[f"AUXILIARY_{_aux_task.upper()}_BASE_URL"] = agent.fallback_base_url
        return env

    def _has_credentials(self, agent: Agent) -> bool:
        if self._provider_uses_sdk_auth(normalize_runtime_provider(agent.provider)):
            return True
        # Platform-level provider env keys are intentionally NOT accepted:
        # they are stripped from the child environment (env_sanitize) so an
        # agent without its own secret ref can never bill the platform.
        return bool(agent.api_key_ref)

    def _provider_uses_sdk_auth(self, provider: str | None) -> bool:
        if not provider:
            return False
        try:
            from hermes_cli.auth import PROVIDER_REGISTRY

            pconfig = PROVIDER_REGISTRY.get(provider)
            auth_type = getattr(pconfig, "auth_type", None) if pconfig else None
            return str(auth_type or "").strip().lower() == "aws_sdk"
        except (ImportError, AttributeError, KeyError):
            logger.debug("Bedrock provider detection failed; falling back to string comparison", exc_info=True)
        return provider == "bedrock"

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

    async def _resolve_effective_model(self, agent: Agent, runtime_provider: str | None) -> str:
        """Resolve the effective model for an agent.

        If use_provider_default is True, look up the provider's default_model.
        Otherwise fall back to agent.model.
        """
        if not getattr(agent, "use_provider_default", False):
            return agent.model or "anthropic/claude-sonnet-4"

        async with self.session_factory() as db:
            from sqlalchemy import select

            from hermeshq.models.provider import ProviderDefinition

            # Find the provider definition matching the agent's runtime provider
            result = await db.execute(
                select(ProviderDefinition).where(ProviderDefinition.runtime_provider == (runtime_provider or ""))
            )
            provider = result.scalar_one_or_none()
            if provider and provider.default_model:
                return provider.default_model

        # Fallback to agent.model if no provider default found
        return agent.model or "anthropic/claude-sonnet-4"
