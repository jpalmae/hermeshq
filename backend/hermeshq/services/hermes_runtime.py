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

    @staticmethod
    def _task_timeout_for(task: Task) -> int:
        from hermeshq.config import get_settings

        limit = HermesRuntime._task_timeout_seconds()
        if str((task.metadata_json or {}).get("source") or "") == "public_chat":
            public_limit = int(get_settings().public_chat_task_timeout_seconds)
            if public_limit > 0:
                return max(60, min(limit, public_limit))
        return limit

    @staticmethod
    def _thinking_overrides(thinking_mode: str | None) -> dict:
        if thinking_mode not in ("on", "off"):
            return {}
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": thinking_mode == "on"}}}

    @staticmethod
    def _merge_overrides(base: dict, extra: dict) -> dict:
        merged = dict(base)
        for key, value in extra.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = HermesRuntime._merge_overrides(merged[key], value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _effective_thinking_mode(agent: Agent, task: Task | None) -> str | None:
        from hermeshq.config import get_settings

        mode = getattr(agent, "thinking_mode", None)
        metadata = (getattr(task, "metadata_json", None) or {}) if task is not None else {}
        if metadata.get("scheduled") and get_settings().scheduled_tasks_thinking == "off":
            return "off"
        return mode

    @staticmethod
    def _build_request_overrides(agent: Agent, hermes_home: Path, task: Task | None = None) -> dict:
        return HermesRuntime._merge_overrides(
            HermesRuntime._thinking_overrides(HermesRuntime._effective_thinking_mode(agent, task)),
            HermesRuntime._load_request_overrides(hermes_home),
        )

    @staticmethod
    def _load_request_overrides(hermes_home: Path) -> dict:
        path = hermes_home / "request_overrides.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable request overrides at %s", path)
            return {}
        if not isinstance(data, dict):
            logger.warning("Ignoring request overrides at %s: expected a JSON object", path)
            return {}
        return data

    @staticmethod
    def _task_idle_timeout_seconds() -> int:
        from hermeshq.config import get_settings

        return max(0, int(get_settings().task_idle_timeout_seconds))

    @staticmethod
    async def _readline_or_stall(stdout: asyncio.StreamReader, idle_timeout: int | None) -> bytes:
        try:
            return await asyncio.wait_for(stdout.readline(), timeout=idle_timeout)
        except TimeoutError as exc:
            raise RuntimeExecutionError(
                f"Hermes runtime produced no output for {idle_timeout}s and was stopped"
            ) from exc

    @staticmethod
    def _model_timeout_env() -> dict[str, str]:
        from hermeshq.config import get_settings

        base = max(30, int(get_settings().model_call_timeout_seconds))
        return {
            "HERMES_API_TIMEOUT": str(base * 3),
            "HERMES_STREAM_READ_TIMEOUT": str(base),
            "HERMES_STREAM_STALE_TIMEOUT": str(base + base // 2),
            "HERMES_API_CALL_STALE_TIMEOUT": str(base * 2),
            "HERMES_STREAM_RETRIES": "1",
        }

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
        policy_master = (task.metadata_json or {}).get("policy_master_agent_id")
        if isinstance(policy_master, str) and policy_master.strip() and policy_master != agent.id:
            process_env["HERMESHQ_POLICY_MASTER"] = policy_master.strip()
        if fallback_override:
            process_env = {**process_env, **self._fallback_env(agent, fallback_override.get("api_key"))}
        enabled_toolsets, disabled_toolsets = resolve_effective_toolsets(
            agent.runtime_profile,
            agent.enabled_toolsets,
            agent.disabled_toolsets,
        )
        runtime_provider = self.installation_manager._model_provider_for_agent(agent)
        effective_base_url = await self.installation_manager._effective_provider_base_url(agent)
        effective_model = await self._resolve_effective_model(agent, runtime_provider)
        # ── File delivery instructions: apply to every task, every channel ──
        # The MEDIA: parser below (which turns these lines into response
        # attachments) runs unconditionally for all tasks. Without telling
        # the model about it here, it has no way to know the convention and
        # falls back to inventing something plausible-but-wrong (e.g. a
        # ChatGPT-style `sandbox:` markdown link, which delivers as dead
        # text instead of a real attachment). This was previously gated to
        # mobile_app tasks only, so every other channel silently failed to
        # deliver generated files. Fixed 2026-09-15.
        runtime_system_prompt = (
            runtime_system_prompt + "\n\n"
            "When you generate a file (PDF, image, spreadsheet, etc.), save it "
            "in your work/ or output/ directory — NOT in /tmp or other system "
            "locations. After generating it, add a line at the END of your "
            "response in the exact format: MEDIA:/path/to/your/file.ext — this "
            "is required for the file to be delivered to the user as a real "
            "attachment. Do NOT use any other link format (e.g. markdown links, "
            "`sandbox:` URIs) to reference generated files — those are not "
            "understood by this platform and will not deliver anything. "
            "Do NOT mention the file path anywhere else in your response text; "
            "the MEDIA: line is stripped automatically and the file is attached "
            "for you."
        )

        # ── Channel routing: suppress external channels for mobile_app tasks ──
        _task_meta = task.metadata_json or {}
        _reply_to = str(_task_meta.get("reply_to") or _task_meta.get("source") or "").strip().lower()
        if _reply_to == "mobile_app":
            runtime_system_prompt = (
                runtime_system_prompt + "\n\n" + "IMPORTANT: You are responding through the SixAgentic mobile app. "
                "Always provide your response directly in the task response text. "
                "Do NOT send responses through Telegram, WhatsApp, email, or any other "
                "external channel."
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

        host_hermes_home = self.installation_manager.build_hermes_home(agent.workspace_path)
        request_overrides = self._build_request_overrides(agent, host_hermes_home, task)
        if request_overrides:
            payload["request_overrides"] = request_overrides

        if fallback_override:
            for key, value in fallback_override.items():
                if value is not None:
                    payload[key] = value

        task_timeout = self._task_timeout_for(task)
        for env_key, env_value in self._model_timeout_env().items():
            process_env.setdefault(env_key, env_value)

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
            idle_timeout = self._task_idle_timeout_seconds() or None
            while True:
                line = await self._readline_or_stall(process.stdout, idle_timeout)
                if not line:
                    break
                await handle_line(line.decode("utf-8", errors="replace").strip())
            return result

        try:
            final_result = await asyncio.wait_for(_consume(), timeout=task_timeout)
        except TimeoutError as exc:
            raise RuntimeExecutionError(f"Hermes runtime exceeded the task timeout of {task_timeout}s") from exc
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    logger.warning("Task runner subprocess %s did not exit after kill", process.pid)

        stderr_output = ""
        return_code: int | None = None
        if process is not None:
            if process.stderr is not None:
                stderr_output = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
            return_code = process.returncode if process.returncode is not None else await process.wait()
            if return_code != 0 and not final_result:
                raise RuntimeExecutionError(stderr_output or "Hermes runtime process exited with an error")
        if not final_result:
            logger.warning(
                "Hermes runtime ended without a result payload (task=%s, returncode=%s, stderr=%s)",
                task.id,
                return_code,
                stderr_output[-1500:] or "<empty>",
            )
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
        import json as _json
        import re as _re
        import shutil as _shutil
        import uuid as _uuid
        from pathlib import Path as _Path

        workspace_path = _Path(payload["cwd"])
        uploads_dir = workspace_path / "uploads"
        workspace_root = workspace_path.resolve()
        isolated_root = _Path(execution_workspace)
        _collected_by_source: dict[str, dict] = {}

        def _collect_attachment(file_path_str: str, *, log_prefix: str) -> dict | None:
            """Copy a generated file into uploads/ and build its attachment
            record. Shared by the MEDIA: parser and the auto-attach fallback
            below, keyed by resolved source path so the same file is never
            attached twice even if the model both calls the tool *and*
            writes a matching MEDIA: line for it.
            """
            file_path = _Path(file_path_str)
            if self.runtime_runner_client is not None and file_path.is_absolute():
                try:
                    file_path = workspace_path / file_path.relative_to(isolated_root)
                except ValueError:
                    logger.warning("%s: path is outside the isolated workspace: %s", log_prefix, file_path_str)
                    return None
            if not file_path.exists():
                file_path = workspace_path / file_path_str
            resolved_file = file_path.resolve()
            if workspace_root not in [resolved_file, *resolved_file.parents]:
                logger.warning("%s: path escapes the agent workspace: %s", log_prefix, file_path_str)
                return None
            file_path = resolved_file
            if not file_path.exists() or not file_path.is_file():
                logger.warning("%s: file not found: %s", log_prefix, file_path_str)
                return None
            resolved = str(file_path)
            if resolved in _collected_by_source:
                return _collected_by_source[resolved]

            ext = file_path.suffix.lower()
            if ext not in _ALLOWED_MEDIA_EXTS:
                logger.warning("%s: file extension not allowed: %s", log_prefix, ext)
                return None

            uploads_dir.mkdir(parents=True, exist_ok=True)
            file_id = str(_uuid.uuid4())
            dest_path = uploads_dir / f"{file_id}{ext}"
            try:
                _shutil.copy2(file_path, dest_path)
            except OSError as exc:
                logger.warning("%s: failed to copy %s: %s", log_prefix, file_path, exc)
                return None

            attachment = {
                "file_id": file_id,
                "filename": file_path.name,
                "media_type": _MEDIA_EXT_MIME_MAP.get(ext, "application/octet-stream"),
                "size": dest_path.stat().st_size,
                "caption": "",
                "source_path": str(file_path),
            }
            _collected_by_source[resolved] = attachment
            logger.info("%s: collected %s (%d bytes) as %s", log_prefix, file_path.name, attachment["size"], file_id)
            return attachment

        media_pattern = _re.compile(r"(?im)^MEDIA:\s*(.+)$")
        media_matches = list(media_pattern.finditer(raw_response))
        for match in media_matches:
            _collect_attachment(match.group(1).strip(), log_prefix="MEDIA")
        if media_matches:
            # Strip MEDIA: lines from the response text
            raw_response = media_pattern.sub("", raw_response).strip()
            # Also strip any remaining inline MEDIA: references
            raw_response = _re.sub(r"(?i)MEDIA:\s*/\S+", "", raw_response).strip()
            # Clean up any double blank lines left behind
            raw_response = _re.sub(r"\n{3,}", "\n\n", raw_response)

        # ── Repair fake link schemes the model invents instead of MEDIA: ──
        # Despite the system prompt instruction, models occasionally emit a
        # markdown link like [file.pdf](sandbox:/abs/path) — a convention
        # from a different platform (ChatGPT Code Interpreter) that means
        # nothing here and renders as a dead link. Once a model does this
        # once in a conversation it tends to repeat the pattern in later
        # turns of the same session even after the instruction is followed
        # correctly elsewhere. Detect it, attach the real file the same way
        # MEDIA: would, and strip the dead link from the text. Added 2026-09-15.
        fake_link_pattern = _re.compile(r"\[([^\]]*)\]\((?:sandbox|file|attachment):(/[^)\s]+)\)")
        fake_link_matches = list(fake_link_pattern.finditer(raw_response))
        for match in fake_link_matches:
            _collect_attachment(match.group(2).strip(), log_prefix="fake-link")
        if fake_link_matches:
            raw_response = fake_link_pattern.sub("", raw_response).strip()
            raw_response = _re.sub(r"\n{3,}", "\n\n", raw_response)

        # ── Auto-attach files from known file-generating tools ──
        # Smaller models don't reliably remember to add a MEDIA: line even
        # when told to (observed ~half the time in practice with e.g.
        # qwen3.6-35b-a3b). For a narrow allowlist of tools whose successful
        # result carries a `path` to a file they just created, attach it
        # directly from the tool result — delivery then doesn't depend on
        # the model repeating the path correctly. Added 2026-09-15.
        _AUTO_ATTACH_TOOLS = {
            "hq_pdf_generate",
            "hq_docx_generate",
            "hq_pptx_generate",
            "hq_pptx_from_template",
            "hq_xlsx_generate",
        }
        task_messages = final_result.get("messages") or []
        _auto_attach_call_ids = {
            tool_call.get("id")
            for msg in task_messages
            if msg.get("role") == "assistant"
            for tool_call in (msg.get("tool_calls") or [])
            if (tool_call.get("function") or {}).get("name") in _AUTO_ATTACH_TOOLS
        }
        if _auto_attach_call_ids:
            for msg in task_messages:
                if msg.get("role") != "tool" or msg.get("tool_call_id") not in _auto_attach_call_ids:
                    continue
                try:
                    tool_result = _json.loads(msg.get("content") or "{}")
                except (ValueError, TypeError):
                    continue
                result_path = tool_result.get("path") if isinstance(tool_result, dict) else None
                if tool_result.get("success") and result_path:
                    _collect_attachment(result_path, log_prefix="auto-attach")

        # ── Leave one canonical marker per attachment in the visible text ──
        # Regardless of which of the three mechanisms above found it (an
        # explicit MEDIA: line, a repaired fake link, or auto-attach from a
        # tool result the model never mentioned), every collected file gets
        # exactly one normalized line appended: `MEDIA: uploads/<uuid>.ext`
        # — a relative path under this task's own uploads/ dir, using the
        # same file_id already used for the structured `attachments` field
        # above. This is the plain-text convention consumers that only see
        # response text (not the structured field) can regex against; agreed
        # with the Teams bridge team 2026-09-16, format: `^MEDIA:\s*uploads/
        # ([0-9a-f-]{36})(\.\w+)\s*$`. Added 2026-09-16.
        if _collected_by_source:
            markers = "\n".join(
                f"MEDIA: uploads/{att['file_id']}{_Path(att['filename']).suffix.lower()}"
                for att in _collected_by_source.values()
            )
            raw_response = f"{raw_response}\n\n{markers}" if raw_response else markers

        # Merge with any attachments the task runner already collected
        runner_attachments = list(final_result.get("response_attachments") or [])
        all_attachments = runner_attachments + list(_collected_by_source.values())

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
