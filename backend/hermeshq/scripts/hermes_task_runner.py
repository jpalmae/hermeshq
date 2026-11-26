from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path


def _diag(mensaje: str) -> None:
    print(f"[runner] {mensaje}", file=sys.stderr, flush=True)


_SALIDA = None


def _abrir_salida() -> None:
    """Guarda una copia propia del canal de salida (fd 1) antes de que el agente toque sys.stdout."""
    global _SALIDA
    try:
        _SALIDA = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1, closefd=True)
    except OSError:
        _SALIDA = None


def _escribir(linea: str) -> None:
    destino = _SALIDA if _SALIDA is not None else sys.stdout
    destino.write(linea + "\n")
    destino.flush()


_IDENTIDAD_EJECUCION: dict | None = None
SOLO_DEL_RUNNER = (
    "HERMESHQ_AGENT_TOKEN",
    "HERMESHQ_ATTEMPT_ID",
    "HERMESHQ_LEASE_GENERATION",
    "HERMESHQ_EXECUTION_ID",
    "HERMESHQ_TASK_PAYLOAD",
)


def _iniciar_latido(fd, clave, intervalo) -> None:
    if not _IDENTIDAD_EJECUCION or not isinstance(fd, int) or not clave:
        return
    try:
        espera = max(0.2, float(intervalo or 10))
        os.set_inheritable(fd, False)
    except (OSError, TypeError, ValueError) as exc:
        _diag(f"no se pudo preparar el canal de latido: {exc}")
        return
    identidad = dict(_IDENTIDAD_EJECUCION)
    base = f"{identidad['execution_id']}|{identidad['attempt_id']}|{identidad['lease_generation']}"
    llave = str(clave).encode()

    def _latir() -> None:
        seq = 0
        while True:
            seq += 1
            ts = f"{time.time():.3f}"
            mac = hmac.new(llave, f"{base}|{seq}|{ts}".encode(), hashlib.sha256).hexdigest()
            linea = json.dumps({**identidad, "seq": seq, "ts": ts, "mac": mac}) + "\n"
            try:
                os.write(fd, linea.encode("utf-8"))
            except OSError:
                return
            time.sleep(espera)

    threading.Thread(target=_latir, name="hq-latido", daemon=True).start()


def _proteger_proceso() -> None:
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)
    except (OSError, AttributeError):
        _diag("no se pudo marcar el runner como no volcable")


def _ocultar_token_a_los_comandos() -> None:
    try:
        import tools.environments.local as entorno_local

        entorno_local._HERMES_PROVIDER_ENV_BLOCKLIST = frozenset(
            entorno_local._HERMES_PROVIDER_ENV_BLOCKLIST | set(SOLO_DEL_RUNNER)
        )
    except ImportError:
        _diag("no se pudo quitar el token del agente del entorno de los comandos")


def _herramienta_terminada(tool_call_id, nombre, args, resultado) -> None:
    _emit({"event": "tool_finished", "name": str(nombre or "tool"), "clase": "sin_clasificar", "ms": 0})


def _emit(payload: dict) -> None:
    if _IDENTIDAD_EJECUCION and payload.get("event") in ("result", "error"):
        payload = {**payload, "execution": dict(_IDENTIDAD_EJECUCION)}
    try:
        _escribir(json.dumps(payload, ensure_ascii=False))
    except (UnicodeEncodeError, ValueError) as exc:
        _diag(f"no se pudo escribir el evento {payload.get('event')} en UTF-8 ({exc}); se reenvía en ASCII")
        _escribir(json.dumps(payload, ensure_ascii=True))


_AVISOS_INTERNOS = (
    re.compile(r"⚠️?\s*Response truncated \(finish_reason='length'\)[^⚠\n]*"),
    re.compile(r"⚠️?\s*Truncated tool call[^\n]*?retrying API call\.\.\."),
    re.compile(r"❌\s*Max retries[^\n]*"),
    re.compile(r"\[Tool loop warning:[^\]]*\]"),
)


def limpiar_aviso_interno(texto: str) -> str:
    """Quita del texto que ve el usuario los avisos internos del motor (cortes por tamaño, reintentos, bucles)."""
    for patron in _AVISOS_INTERNOS:
        texto = patron.sub("", texto)
    return texto


def texto_final(result: dict, emitted_chunks: list) -> str:
    final = result.get("final_response")
    if isinstance(final, str) and final.strip():
        return final.strip()
    return "".join(chunk for chunk in emitted_chunks if isinstance(chunk, str)).strip()


def _extract_tool_calls(messages: list[dict]) -> list[dict]:
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


def _activar_guardia(payload: dict) -> None:
    if not payload.get("workspace_guard", True):
        return
    try:
        import importlib.util

        carpeta = Path(__file__).resolve().parent / "guard_site"
        spec = importlib.util.spec_from_file_location("hermeshq_workspace_guard", carpeta / "workspace_guard.py")
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        os.environ["HERMES_SESSION_CWD"] = os.environ.get("HERMES_SESSION_CWD") or payload["cwd"]
        modulo.install(payload["cwd"])
        previo = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = str(carpeta) + (os.pathsep + previo if previo else "")
    except Exception as exc:  # noqa: BLE001  # a failure to enable the guard must be visible but must not stop the task
        _emit({"event": "warning", "warning": f"could not enable the workspace guard: {exc}"})


def _activar_pensamiento_adaptativo() -> None:
    """Thinking stays on for planning calls and is switched off for the calls that only digest tool results."""
    try:
        from openai.resources.chat.completions import Completions

        original = Completions.create

        def create(self, *args, **kwargs):
            mensajes = kwargs.get("messages") or []
            if mensajes and isinstance(mensajes[-1], dict) and mensajes[-1].get("role") == "tool":
                extra = dict(kwargs.get("extra_body") or {})
                plantilla = dict(extra.get("chat_template_kwargs") or {})
                plantilla["enable_thinking"] = False
                extra["chat_template_kwargs"] = plantilla
                kwargs["extra_body"] = extra
            return original(self, *args, **kwargs)

        Completions.create = create
    except Exception as exc:  # noqa: BLE001  # without the patch the run simply keeps thinking on
        _emit({"event": "warning", "warning": f"could not enable adaptive thinking: {exc}"})


def main() -> int:
    _proteger_proceso()
    _abrir_salida()
    # Payload arrives via stdin (keeps the API key out of the process
    # environment, where it would be visible via /proc/<pid>/environ).
    # Fall back to the legacy env var for backwards compatibility.
    payload_raw = ""
    if not sys.stdin.isatty():
        try:
            payload_raw = sys.stdin.read().strip()
        except OSError:
            payload_raw = ""
    if not payload_raw:
        payload_raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    if not payload_raw:
        _emit({"event": "error", "error": "Missing task payload (stdin or HERMESHQ_TASK_PAYLOAD)"})
        return 1

    payload = json.loads(payload_raw)
    global _IDENTIDAD_EJECUCION
    identidad = payload.get("execution")
    if isinstance(identidad, dict) and identidad.get("attempt_id"):
        _IDENTIDAD_EJECUCION = {
            "execution_id": identidad.get("execution_id"),
            "attempt_id": identidad.get("attempt_id"),
            "lease_generation": identidad.get("lease_generation"),
        }
        _iniciar_latido(
            identidad.get("heartbeat_fd"), identidad.pop("heartbeat_key", None), identidad.get("heartbeat_interval")
        )
        _emit({"event": "attempt_started", "execution": dict(_IDENTIDAD_EJECUCION)})
        os.environ["HERMESHQ_EXECUTION_ID"] = str(identidad.get("execution_id") or "")
        os.environ["HERMESHQ_ATTEMPT_ID"] = str(identidad.get("attempt_id") or "")
        os.environ["HERMESHQ_LEASE_GENERATION"] = str(identidad.get("lease_generation") or "")
        credencial = identidad.pop("agent_token", None)
        if credencial:
            os.environ["HERMESHQ_AGENT_TOKEN"] = str(credencial)
    _ocultar_token_a_los_comandos()
    os.environ["HERMES_HOME"] = payload["hermes_home"]
    os.environ["HERMES_QUIET"] = "1"
    os.environ["HERMESHQ_TASK_PAYLOAD"] = json.dumps(
        {"metadata": payload.get("metadata") or {}, "task_id": payload.get("task_id")}
    )
    os.environ.setdefault("TERM", "xterm-256color")

    os.chdir(payload["cwd"])
    _activar_guardia(payload)
    if payload.get("thinking_adaptive"):
        _activar_pensamiento_adaptativo()

    # ── Plugin discovery ────────────────────────────────────────
    # One-shot task subprocesses construct AIAgent directly and never enter a
    # hermes_cli entrypoint, so user plugins (including hermeshq_guard) are not
    # discovered and their hooks never register. Force discovery against the
    # agent home so policy enforcement and telemetry run in tasks too.
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered(force=True)
    except Exception as _plugin_err:  # noqa: BLE001  # plugins are best-effort in tasks
        _emit({"event": "warning", "warning": f"plugin discovery failed: {_plugin_err}"})

    # ── Auxiliary client env injection ─────────────────────────
    # When HermesHQ passes explicit api_key + base_url, the main agent
    # client is constructed directly. But the auxiliary clients (vision,
    # compression, etc.) resolve credentials independently via env vars.
    # Inject the agent's credentials so auxiliary tasks can use them too.
    _api_key = payload.get("api_key")
    _base_url = payload.get("base_url")
    if _api_key and _base_url:
        os.environ.setdefault("OPENAI_API_KEY", _api_key)
        os.environ.setdefault("OPENAI_BASE_URL", _base_url)
        for _task in ("vision", "compression", "session_search", "web_extract"):
            os.environ.setdefault(f"AUXILIARY_{_task.upper()}_API_KEY", _api_key)
            os.environ.setdefault(f"AUXILIARY_{_task.upper()}_BASE_URL", _base_url)
    # ── End auxiliary client env injection ─────────────────────

    # ── Attachment enrichment ──────────────────────────────────
    task_metadata = payload.get("metadata", {})
    attachments = task_metadata.get("attachments", [])
    if attachments:
        attachment_lines = []
        for att in attachments:
            att_path = att.get("path", "")
            if att_path:
                full_path = Path(payload["cwd"]) / att_path
                if full_path.exists():
                    line = f"- [{att.get('media_type', 'file')}] {att_path}"
                    if att.get("filename"):
                        line += f" (filename: {att['filename']})"
                    if att.get("caption"):
                        line += f" — {att['caption']}"
                    attachment_lines.append(line)
        if attachment_lines:
            payload["prompt"] += "\n\nAttached files:\n" + "\n".join(attachment_lines)
    # ── End attachment enrichment ──────────────────────────────

    try:
        from run_agent import AIAgent

        emitted_chunks: list[str] = []

        def on_delta(delta) -> None:
            if delta is None:
                _emit({"event": "segment_break"})
                return
            if not isinstance(delta, str):
                return
            delta = limpiar_aviso_interno(delta)
            if not delta:
                return
            emitted_chunks.append(delta)
            _emit({"event": "delta", "data": delta})

        def on_tool_progress(status, name=None, *rest, **kwargs) -> None:
            if status == "tool.started":
                _emit({"event": "tool_started", "name": str(name or "tool")})

        agent = AIAgent(
            model=payload["model"],
            provider=payload.get("provider"),
            base_url=payload.get("base_url"),
            api_key=payload.get("api_key"),
            session_id=payload.get("session_id"),
            quiet_mode=True,
            enabled_toolsets=payload.get("enabled_toolsets") or None,
            disabled_toolsets=payload.get("disabled_toolsets") or None,
            ephemeral_system_prompt=payload.get("system_prompt"),
            request_overrides=payload.get("request_overrides") or None,
            max_iterations=payload.get("max_iterations", 90),
            skip_context_files=False,
            skip_memory=False,
            platform="hermeshq",
            stream_delta_callback=on_delta,
            tool_progress_callback=on_tool_progress,
            tool_complete_callback=_herramienta_terminada,
        )

        result = agent.run_conversation(
            user_message=payload["prompt"],
            task_id=payload["task_id"],
            system_message=payload.get("system_override"),
            conversation_history=payload.get("conversation_history") or None,
        )

        messages = result.get("messages", [])
        _diag(
            f"conversación terminada: partial={result.get('partial')} completed={result.get('completed')} "
            f"claves={sorted(result)} mensajes={len(result.get('messages') or [])}"
        )
        if result.get("partial") and not result.get("final_response"):
            raise RuntimeError(f"Hermes runtime stopped early: {result.get('error') or 'partial result'}")
        final_response = texto_final(result, emitted_chunks)
        tool_calls = _extract_tool_calls(messages)
        assistant_messages = [message for message in messages if message.get("role") == "assistant"]
        if not final_response and not assistant_messages and not tool_calls:
            raise RuntimeError("Hermes runtime returned no assistant output")

        _diag("emitiendo el resultado final")
        _emit(
            {
                "event": "result",
                "final_response": final_response,
                "messages": messages,
                "tool_calls": tool_calls,
                "tokens_used": max(256, len(str(result).split())),
                "iterations": len(assistant_messages),
                "engine": "hermes-agent",
            }
        )
        return 0
    except Exception as exc:  # noqa: BLE001  # CLI top-level catch-all for error reporting
        _emit(
            {
                "event": "error",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc(),
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
