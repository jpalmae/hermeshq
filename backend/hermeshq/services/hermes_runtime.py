import asyncio
import json
import logging
import re
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.task import Task
from hermeshq.services.background_style import REGLA_FUENTES, REGLA_HERRAMIENTAS, REGLA_VERIFICACION
from hermeshq.services.credentials import require_secret_value
from hermeshq.services.execution_lease import (
    ExecutionLeaseError,
    ExecutionLeaseStore,
    IdentityUnconfirmedError,
    Lease,
    LeaseLostError,
    LocalRunnerRegistry,
    lease_settings,
)
from hermeshq.services.hermes_installation import HermesInstallationManager
from hermeshq.services.permission_enforcer import PermissionEnforcer, enforceable_egress_domains, llm_egress_hosts
from hermeshq.services.provider_catalog import normalize_runtime_provider
from hermeshq.services.runner_heartbeat import HeartbeatChannel
from hermeshq.services.runtime_base import RuntimeBase, RuntimeExecutionError, RuntimeExecutionResult
from hermeshq.services.runtime_profiles import resolve_effective_toolsets
from hermeshq.services.runtime_runner_client import RuntimeRunnerClient
from hermeshq.services.secret_vault import SecretVault
from hermeshq.services.toolsets_pedido import exposicion_activa, filtrar_para_tarea, seleccion_progresiva_detallada

logger = logging.getLogger(__name__)
DEFAULT_REQUEST_OVERRIDES = {"parallel_tool_calls": False}


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


_tareas_reserva: set = set()


def memoria_persistente_para(metadata) -> bool:
    md = metadata if isinstance(metadata, dict) else {}
    if md.get("conversation") or md.get("background_job") or md.get("source") == "background_job":
        return False
    return not (md.get("thread_user_id") and not md.get("scheduled"))


def _workspace_isolation_enabled() -> bool:
    from hermeshq.config import get_settings

    return bool(get_settings().workspace_isolation)


END_REASON_VOCAB = (
    "completed",
    "timeout",
    "idle_timeout",
    "runtime_error",
    "invalid_tool_calls",
    "cancelled",
    "queue_timeout",
    "stopped_early",
)
_TRACE_TAIL_MAX_CHARS = 2000


def _classify_end_reason(message: str) -> str:
    texto = (message or "").lower()
    if "produced no output" in texto:
        return "idle_timeout"
    if "exceeded the task timeout" in texto:
        return "timeout"
    if "invalid tool call" in texto:
        return "invalid_tool_calls"
    if "stopped early" in texto:
        return "stopped_early"
    return "runtime_error"


def _safe_trace_tail(text: str, api_key: str | None) -> str:
    texto = str(text or "")
    if api_key:
        from hermeshq.services.audit import REDACTED

        texto = texto.replace(api_key, REDACTED)
    return texto[-_TRACE_TAIL_MAX_CHARS:]


def _build_partial(traza: dict, end_reason: str | None, stderr_tail: str, api_key: str | None) -> dict:
    herramientas = list(traza.get("herramientas") or [])
    error_evento = traza.get("error_evento") or {}
    pasos = int(traza.get("pasos") or 0)
    trace_source = error_evento.get("traceback") or stderr_tail or ""
    return {
        "end_reason": end_reason or "runtime_error",
        "tool_calls": herramientas,
        "iterations": pasos,
        "error_type": str(error_evento.get("error_type") or ""),
        "error_trace": _safe_trace_tail(trace_source, api_key),
        "run": {
            "events": dict(traza.get("eventos") or {}),
            "tools": len(herramientas),
            "steps": pasos,
            "lines": int(traza.get("lineas") or 0),
            "usage": error_evento.get("usage") or {},
        },
    }


def _attach_partial(
    exc: BaseException, traza: dict, end_reason: str | None, stderr_tail: str, api_key: str | None
) -> None:
    try:
        exc.hq_partial = _build_partial(traza, end_reason, stderr_tail, api_key)
    except Exception:  # noqa: BLE001
        logger.debug("Could not attach partial-run telemetry", exc_info=True)


PLATFORM_RULES = (
    "# REGLAS DE LA PLATAFORMA (valen para todos los agentes)\n"
    "- Razonamiento breve: razona en un máximo de 5 líneas antes de actuar. No planifiques ni redactes de antemano "
    "informes, documentos o código dentro de tu razonamiento: escríbelo directo en la llamada a la herramienta. "
    "Si dudas entre dos opciones, elige la más simple y continúa.\n"
    "- Códigos, identificadores, rutas y destinatarios: cópialos exactamente del resultado de la herramienta; "
    "nunca los reescribas de memoria.\n"
    "- Antes de escribir un script revisa si una herramienta o skill ya hace el trabajo. No uses execute_code para "
    "leer archivos, listar carpetas ni buscar texto.\n"
    "- Si una herramienta falla dos veces con el mismo error, no leas su código ni pruebes variantes: dile al usuario "
    "qué falló y qué alternativa hay, o regístralo con hq_solicitar_capacidad.\n"
    "- Menos pasos: para varios archivos, varias búsquedas, varias skills, varias ediciones de un archivo o varias "
    "personas usa UNA llamada con listas (hq_leer_varios, hq_buscar_varios, hq_skills_ver, hq_editar_varios, "
    "ms365_find_person con names, sharepoint_search con queries, sharepoint_read_file y sharepoint_download_file con "
    "items) en vez de repetir la herramienta. En execute_code agrupa inspeccionar, procesar y "
    "verificar en un solo script. Para revisar visualmente un HTML usa hq_html_capturar (una llamada) y mira las "
    "imágenes una vez con vision_analyze; no pruebes navegadores ni conversores.\n"
    "- Si lo pedido va a tardar más de unos 10 minutos (informes grandes, muchos archivos), ofrece hacerlo en "
    "segundo plano con hq_trabajo_largo y avisa al usuario.\n"
    "- Si el primer mensaje de una conversación trae una instrucción o pedido, ejecútalo de inmediato; nunca "
    "respondas solo con un saludo ni ignores lo que se pidió.\n"
    "- Cada mensaje nuevo del usuario puede ser un pedido distinto. Atiende SIEMPRE lo que pide el mensaje actual; "
    "no sigas con una tarea anterior que quedó inconclusa ni arrastres su tema, salvo que el usuario se refiera "
    "explícitamente a ella (p. ej. «sigue», «termínalo», «y ese informe»). Si el pedido actual es de otro tipo o "
    "tema que el anterior, haz el actual y no lo confundas con el previo.\n"
    "- Un mensaje corto de continuación («ejecuta», «dale», «hazlo», «sí», «ok», «continúa», «procede», «ya») SÍ se "
    "refiere a la última acción concreta que quedó pendiente en ESTA conversación: retómala (por ejemplo, generar el "
    "archivo o ejecutar el paso que tú mismo anunciaste como siguiente). Nunca interpretes esos comandos cortos como "
    "un pedido nuevo de otro tema ni abras otra skill por tu cuenta (agenda, reuniones, correos, informes): si no "
    "tienes clara cuál es la acción pendiente, pregúntalo en una sola línea («¿Ejecuto {acción}?») en vez de "
    "adivinar.\n"
    "- Envío de correos: no afirmes que enviaste, reenviaste ni que un correo «quedó en enviados» salvo que la "
    "herramienta de envío (ms365_mail_send, ms365_mail_forward o ms365_mail_reply) haya devuelto éxito en este "
    "mismo turno. Si falló, no la llamaste o solo mostraste una vista previa, dilo con claridad; nunca inventes "
    "confirmaciones. Distingue «lo envié» (lo confirma el éxito de la herramienta) de «le llegó / lo recibió» (eso "
    "NO lo puedes confirmar: no lo afirmes). Para REENVIAR usa ms365_mail_forward y para RESPONDER usa "
    "ms365_mail_reply (reply_all para conservar a los de copia, con body+inline_images para la firma): ambas toman "
    "los destinatarios del hilo, así que NO los re-escribas ni crees un correo nuevo con send. Al aprobar un envío "
    "(«envía»), vuelve a llamar con los destinatarios EXACTAMENTE iguales a la vista previa; nunca cambies ni "
    "adivines una dirección (p. ej. julia.mora@ ↔ jmora@) entre la vista previa y el envío, o el candado "
    "la rechaza y vuelve a pedir aprobación.\n"
    "- Honestidad en TODA acción de salida o escritura (no solo correo): no afirmes que agendaste, actualizaste, "
    "invitaste, eliminaste, cancelaste, compartiste, enviaste por Teams, marcaste como leído ni delegaste salvo que "
    "la herramienta correspondiente haya devuelto éxito en ESTE mismo turno. Si solo mostraste una vista previa o la "
    "acción quedó pendiente de tu aprobación, dilo como pendiente, no como hecho. Distingue «lo hice» (lo confirma el "
    "éxito de la herramienta) de «le llegó» (eso no lo puedes confirmar). Al reportar, cita las cantidades exactas que "
    "devolvió la herramienta (p. ej. cuántos correos marcó), nunca un número de memoria.\n"
    "- Acciones que salen hacia afuera o cancelan (enviar correo, crear/editar/eliminar una cita, mensaje de Teams): la "
    "herramienta te devuelve primero una VISTA PREVIA sin ejecutar. Muéstrasela al usuario con los destinatarios/"
    "asistentes exactos (incluido quién recibe una cancelación al editar o eliminar) y pregúntale; solo cuando él diga "
    "que sí, vuelve a llamar con aprobado_por_el_usuario=true y EXACTAMENTE los mismos datos. Nunca pongas "
    "aprobado_por_el_usuario=true en la misma respuesta en que generaste la vista previa.\n"
    "- Citas de calendario y asistentes: nunca inventes ni adivines correos ni asistentes. Para crear o reenviar "
    "un evento «con los mismos asistentes», primero LEE la invitación real (ms365_mail_get del correo o su adjunto "
    ".ics) y copia los correos tal cual, aunque sean externos (otro dominio, p. ej. @otraempresa.example): esos son válidos "
    "como asistentes, no los reemplaces por cuentas internas. Si no logras obtener los asistentes reales, crea el "
    "evento SOLO con el usuario y dilo («no agregué asistentes porque no pude leerlos; dime a quién invito»); jamás "
    "construyas una dirección a partir de un apellido ni asumas el dominio interno. El roster «Known agents» es solo "
    "para comunicación entre agentes, nunca una fuente de destinatarios de correo o calendario. Antes de que salga "
    "cualquier invitación o correo, muestra la lista exacta de destinatarios; «crea la cita» no es permiso para "
    "invitar a nadie. Copia fecha, hora y duración exactas de la fuente.\n"
    "- Adjuntos a destinatarios externos: los filtros de correo suelen bloquear los adjuntos .html (riesgo de "
    "phishing). Para enviar un informe o página HTML a alguien externo, manda el PDF (o una imagen) y comparte el "
    "HTML por enlace (OneDrive/SharePoint), no como adjunto .html; avísale al usuario que el .html puede no llegar.\n"
    "- No inventes causas ni explicaciones. Si algo falla, no puedes hacerlo o no sabes por qué, NO inventes un "
    "motivo que suene plausible (p. ej. «esa cita es de Google, no de Outlook» o «el resultado del cron solo se ve "
    "en el web chat»). Di el error real que devolvió la herramienta, o «no tengo una herramienta para esto», o «no "
    "estoy seguro de por qué»; funda toda explicación en hechos verificables (el tipo de objeto, la salida real de "
    "la herramienta), nunca en suposiciones. Si no lo leíste o verificaste, no afirmes su causa.\n"
    "- Invitaciones de reunión: un correo de invitación que llegó a tu buzón Microsoft 365 (tipo eventMessageRequest) "
    "SÍ es una invitación de Outlook, aunque la reunión use Google Meet. Para aceptarla, marcarla como provisional o "
    "rechazarla usa ms365_calendar_respond_event (con el event_id o el message_id del correo); no digas que «no se "
    "puede» ni crees un evento de respaldo a mano.\n"
    "- No fabriques contenido que el usuario no pidió. Cada correo, destinatario, asunto, cita o tema que produzcas "
    "debe salir del mensaje del usuario EN ESTA conversación o de un correo/documento que de verdad leíste; nunca "
    "inventes un destinatario, un asunto ni un correo entero (p. ej. «Re: Solicitud de acceso» a alguien que "
    "nadie mencionó). Si el usuario solo te da un dato o una corrección (p. ej. «el correo de X es …»), aplícalo al "
    "borrador o la tarea VIGENTE —no saltes a otro tema ni generes un entregable distinto—; si no hay nada concreto "
    "que redactar, dilo y pregunta, no rellenes con algo inventado.\n"
    "- Memoria: guarda en memoria solo lo que el usuario te pida recordar explícitamente («recuerda…», «de ahora en "
    "adelante…»), no un dato dicho al pasar en medio de una tarea. NUNCA guardes ni uses como «su correo» una "
    "dirección que contradiga la cuenta Microsoft 365 verificada desde la que el usuario envía: si menciona otra "
    "dirección, trátala como un destinatario puntual de ese correo, no como su identidad, y confírmalo antes de "
    "cambiar nada."
)


def _shared_media_roots() -> list[Path]:
    from hermeshq.config import get_settings

    branding = getattr(get_settings(), "branding_root", None)
    return [Path(branding)] if branding else []


def ruta_interna_de_skills(ruta) -> bool:
    return "/.hermes/skills/" in str(ruta or "").replace("\\", "/")


def extraccion_interna(ruta) -> bool:
    partes = [p for p in str(ruta or "").replace("\\", "/").split("/") if p]
    return (
        len(partes) >= 2
        and "work" in partes[:-1]
        and re.fullmatch(r"datos(_[\w-]+)?\.json", partes[-1].lower()) is not None
    )


def media_path_allowed(file_path: Path, workspace: Path, extra_roots: list[Path] | None = None) -> bool:
    try:
        resolved = file_path.resolve()
        workspace_resolved = workspace.resolve()
    except (OSError, RuntimeError):
        return False
    if any(part.lower().startswith(".env") for part in resolved.parts):
        return False
    if workspace_resolved in resolved.parents:
        relative = resolved.relative_to(workspace_resolved).parts
        if relative[0] == ".hermes":
            return len(relative) > 2 and relative[1].endswith("cache")
        return True
    for root in extra_roots or []:
        try:
            if root.resolve() in resolved.parents:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def ultimas_versiones(rutas: list[str]) -> list[str]:
    import re as _re

    ultima: dict[tuple, str] = {}
    for ruta in rutas:
        p = Path(ruta)
        raiz = p.stem
        while True:
            sin = _re.sub(r"(-([2-9]|[1-9]\d{1,2})|_v\d{1,3}|_editado)$", "", raiz, flags=_re.IGNORECASE)
            if sin == raiz or not sin:
                break
            raiz = sin
        ultima[(str(p.parent), raiz.lower(), p.suffix.lower())] = ruta
    elegidas = set(ultima.values())
    return [r for r in dict.fromkeys(rutas) if r in elegidas]


def registrar_nombre_original(uploads_dir, guardado: str, original: str) -> None:
    import json as _json
    import os as _os

    ruta = uploads_dir / ".nombres.json"
    try:
        nombres = _json.loads(ruta.read_text(encoding="utf-8")) if ruta.is_file() else {}
    except (OSError, ValueError):
        nombres = {}
    if not isinstance(nombres, dict):
        nombres = {}
    try:
        nombres[guardado] = original
        if len(nombres) > 2000:
            nombres = dict(list(nombres.items())[-2000:])
        tmp = ruta.with_suffix(".tmp")
        tmp.write_text(_json.dumps(nombres, ensure_ascii=False), encoding="utf-8")
        _os.replace(tmp, ruta)
    except (OSError, ValueError):
        logger.warning("No se pudo registrar el nombre original de %s", guardado)


def _guardia_desbocada() -> dict:
    from hermeshq.config import get_settings

    ajustes = get_settings()
    return {
        "activa": bool(ajustes.runaway_guard_enabled),
        "max_caracteres": int(ajustes.runaway_guard_max_chars),
        "max_segundos": int(ajustes.runaway_guard_max_seconds),
        "max_compactaciones": int(ajustes.runaway_guard_max_compactions),
    }


def _guardia_accion_narrada_activa() -> bool:
    from hermeshq.config import get_settings

    return bool(get_settings().accion_narrada_guard)


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
        self.leases = ExecutionLeaseStore(session_factory)
        self.local_runners = LocalRunnerRegistry()

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
        if (task.metadata_json or {}).get("background_job"):
            return max(60, min(3600, int(get_settings().background_job_timeout_seconds)))
        if str((task.metadata_json or {}).get("source") or "") == "public_chat":
            settings = get_settings()
            public_limit = int(settings.public_chat_task_timeout_seconds)
            if public_limit <= 0:
                return limit
            techo = min(limit, public_limit)
            presupuesto = int(getattr(settings, "public_chat_total_budget_seconds", 0) or 0)
            if presupuesto > 0:
                techo = min(techo, presupuesto - HermesRuntime._segundos_en_cola(task))
            return max(60, techo)
        return limit

    @staticmethod
    def _segundos_en_cola(task: Task) -> int:
        queued, started = getattr(task, "queued_at", None), getattr(task, "started_at", None)
        if queued is None or started is None:
            return 0
        try:
            return max(0, int((started - queued).total_seconds()))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _previous_files_note(
        workspace_path: Path, max_age_hours: int = 3, limit: int = 8, referenciados_en: str | None = None
    ) -> str:
        """Lista los archivos de turnos anteriores que siguen en el workspace, para que el agente no los vuelva a descargar ni a extraer."""
        import time

        limite = time.time() - max(1, max_age_hours) * 3600
        candidatos: list[tuple[float, str]] = []
        patrones = (
            "sharepoint_downloads/*",
            "work/*/datos.md",
            "work/*/datos.json",
            "work/*/spec.json",
            "work/*/*.xlsx",
            "work/*/*.json",
        )
        vistos: set[str] = set()
        for patron in patrones:
            for ruta in workspace_path.glob(patron):
                try:
                    if not ruta.is_file() or ruta.name.startswith("."):
                        continue
                    mtime = ruta.stat().st_mtime
                except OSError:
                    continue
                relativa = str(ruta.relative_to(workspace_path))
                if referenciados_en is not None and relativa not in referenciados_en:
                    continue
                if mtime >= limite and relativa not in vistos:
                    vistos.add(relativa)
                    candidatos.append((mtime, relativa))
        if not candidatos:
            return ""
        ahora = time.time()
        candidatos.sort(reverse=True)
        lineas = [f"- {ruta} (hace {max(1, int((ahora - mtime) // 60))} min)" for mtime, ruta in candidatos[:limit]]
        return (
            "[Archivos de turnos anteriores que siguen en tu workspace. Reutilízalos: NO los vuelvas a buscar, descargar "
            "ni extraer de nuevo; léelos directamente desde aquí]\n" + "\n".join(lineas)
        )

    @staticmethod
    def _prompt_with_notices(prompt: str, metadata, workspace_path: Path) -> str:
        prompt = str(prompt or "")
        notas = []
        if isinstance(metadata, dict) and (metadata.get("conversation") or metadata.get("scheduled")):
            from hermeshq.services.cron_schedule import DEFAULT_TIMEZONE, current_time_context

            notas.append(current_time_context(DEFAULT_TIMEZONE))
        nota = HermesRuntime._nota_reinicio_automatico(metadata, workspace_path)
        if nota:
            notas.append(nota)
        return "\n\n".join([prompt, *notas]) if notas else prompt

    @staticmethod
    def _nota_reinicio_automatico(metadata, workspace_path: Path) -> str:
        aviso = (metadata or {}).get("auto_reset_aviso")
        if not isinstance(aviso, dict):
            return ""
        archivos = ""
        nota = (
            f"[AVISO DEL SISTEMA: las últimas {aviso.get('fallos')} solicitudes de esta conversación agotaron el tiempo "
            f"({aviso.get('minutos')} min perdidos) y el contexto se reinició automáticamente para romper el bucle. "
            "Tu primera línea debe decirle al usuario, sin culparlo, que las últimas solicitudes se agotaron y que empiezas "
            "con contexto limpio. NO retomes planes anteriores. Antes de actuar consulta `catalogo-de-capacidades`: si ya "
            "existe una herramienta (informes, dashboards, informe HSE, datos de Excel, presentaciones, documentos), úsala. "
            "Si es una construcción grande que ninguna herramienta cubre, ofrece hacerla en segundo plano con "
            "`hq_trabajo_largo`. Si no queda claro qué quiere el usuario, pregúntale en UNA frase.]"
        )
        return f"{nota}\n\n{archivos}" if archivos else nota

    _ANON_PUBLIC_USER = "__public_chat__"

    @staticmethod
    def _sin_dueno_definido(metadata) -> bool:
        meta = metadata if isinstance(metadata, dict) else {}
        if meta.get("source") != "public_chat":
            return False
        return str(meta.get("thread_user_id") or "") in ("", HermesRuntime._ANON_PUBLIC_USER)

    @staticmethod
    def _prompt_with_previous_files(
        prompt: str, workspace_path: Path, conversation_history: list | None, metadata=None
    ) -> str:
        prompt = str(prompt or "")
        if not conversation_history:
            return prompt
        if HermesRuntime._sin_dueno_definido(metadata):
            return prompt
        texto_historial = " ".join(
            str(item.get("content") or "") for item in conversation_history if isinstance(item, dict)
        )
        try:
            nota = HermesRuntime._previous_files_note(workspace_path, referenciados_en=texto_historial)
        except OSError:
            return prompt
        return f"{prompt}\n\n{nota}" if nota else prompt

    @staticmethod
    def _recover_result_from_stream(segmentos: list[str], return_code: int | None) -> dict | None:
        """Si el proceso terminó bien pero no entregó el resultado final, usa el último mensaje que escribió el agente."""
        if return_code not in (0, None):
            return None
        texto = next((t.strip() for t in reversed(segmentos) if t.strip()), "")
        if len(texto) < 20:
            return None
        return {
            "final_response": texto,
            "messages": [],
            "tool_calls": [],
            "tokens_used": max(256, len(texto.split())),
            "iterations": 0,
            "engine": "hermes-agent",
            "recovered": True,
        }

    @staticmethod
    def _with_platform_rules(prompt: str) -> str:
        rules = PLATFORM_RULES
        if "RAZONAMIENTO BREVE" in (prompt or ""):
            rules = rules.replace(rules[rules.index("- Razonamiento breve") : rules.index("- Códigos")], "")
        return (prompt or "").rstrip() + "\n\n" + rules

    @staticmethod
    def _thinking_overrides(thinking_mode: str | None) -> dict:
        if thinking_mode not in ("on", "off", "adaptive"):
            return {}
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": thinking_mode != "off"}}}

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
            HermesRuntime._merge_overrides(
                dict(DEFAULT_REQUEST_OVERRIDES),
                HermesRuntime._thinking_overrides(HermesRuntime._effective_thinking_mode(agent, task)),
            ),
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

    async def _registrar_inicio_herramienta(self, lease: Lease | None, agent: Agent, nombre: str) -> None:
        if lease is None:
            return
        try:
            await self.leases.registrar_inicio_herramienta(lease, agent.id, nombre)
        except Exception:  # noqa: BLE001
            logger.exception("Intento %s: no se pudo registrar el inicio de %s", lease.attempt_id, nombre)

    async def _registrar_fin_herramienta(
        self,
        lease: Lease | None,
        agent: Agent,
        nombre: str,
        clase: str,
        duracion_ms: int,
        auditoria: dict | None = None,
    ) -> None:
        if lease is None:
            return
        try:
            await self.leases.registrar_fin_herramienta(
                lease, agent.id, nombre, clase, duracion_ms, auditoria=auditoria
            )
        except Exception:  # noqa: BLE001
            logger.exception("Intento %s: no se pudo registrar el fin de %s", lease.attempt_id, nombre)

    async def _registrar_activacion_herramienta(self, lease: Lease | None, agent: Agent, nombre: str) -> None:
        if lease is None:
            return
        try:
            await self.leases.registrar_activacion_herramienta(lease, agent.id, nombre)
        except Exception:  # noqa: BLE001
            logger.exception("Intento %s: no se pudo registrar la activación de %s", lease.attempt_id, nombre)

    @staticmethod
    def _accumulate_trace_event(traza: dict, event: dict) -> None:
        tipo = event.get("event")
        if tipo == "tool":
            traza.setdefault("herramientas", []).append(
                {
                    "name": str(event.get("name") or "tool"),
                    "status": "completed" if event.get("ok", True) else "error",
                    "duration_ms": int(event.get("ms") or 0),
                }
            )
        elif tipo == "step":
            n = event.get("n")
            if isinstance(n, int):
                traza["pasos"] = max(int(traza.get("pasos") or 0), n)

    @staticmethod
    def _registrar_no_json(traza: dict, text: str, task_id) -> None:
        traza["no_json"] += 1
        traza["ultima_no_json"] = f"{text[:80]} … {text[-80:]} ({len(text)} chars)"
        logger.info("runner: línea fuera del protocolo (task=%s): %s", task_id, text[:200])

    @staticmethod
    async def _readline_or_stall(stdout: asyncio.StreamReader, idle_timeout: int | None) -> bytes:
        try:
            return await asyncio.wait_for(stdout.readline(), timeout=idle_timeout)
        except TimeoutError as exc:
            error = RuntimeExecutionError(f"Hermes runtime produced no output for {idle_timeout}s and was stopped")
            error.hq_end_reason_hint = "idle_timeout"
            raise error from exc

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
        execution_id = str(uuid4())
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
                execution_id=execution_id,
                attempt_number=1,
            )
        except ExecutionLeaseError:
            raise
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
                    execution_id=execution_id,
                    attempt_number=2,
                    fallback_override={
                        "model": agent.fallback_model,
                        "provider": fallback_provider_normalized,
                        "base_url": agent.fallback_base_url,
                        "api_key": fallback_api_key,
                    },
                )
            except Exception as fallback_exc:  # noqa: BLE001  # fallback failed — chain both errors
                if isinstance(fallback_exc, ExecutionLeaseError):
                    raise
                combined = RuntimeExecutionError(f"Primary and fallback providers failed: {fallback_exc}")
                combined.__cause__ = primary_exc
                partial = getattr(fallback_exc, "hq_partial", None) or getattr(primary_exc, "hq_partial", None)
                if partial is not None:
                    combined.hq_partial = partial
                raise combined
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
        runtime_version: str | None = None,
        conversation_history: list[dict] | None = None,
        session_id: str | None = None,
        fallback_override: dict | None = None,
        execution_id: str | None = None,
        attempt_number: int = 1,
    ) -> RuntimeExecutionResult:
        lease = await self.leases.begin(
            str(task.id),
            execution_id or str(uuid4()),
            attempt_number,
            lease_settings()["ttl"],
        )
        try:
            result = await self._run_runner(
                agent,
                task,
                stream_callback,
                api_key,
                runtime_system_prompt,
                runtime_python_bin,
                conversation_history=conversation_history,
                session_id=session_id,
                fallback_override=fallback_override,
                lease=lease,
                runtime_version=runtime_version,
            )
        except asyncio.CancelledError:
            await self.leases.close_quietly(lease, "cancelled", end_reason="cancelled")
            raise
        except LeaseLostError:
            await self.leases.mark_lost(lease, "lease_perdido")
            raise
        except BaseException as exc:
            await self.leases.close_quietly(
                lease, "failed", end_reason=getattr(exc, "hq_end_reason_hint", None) or "runtime_error"
            )
            raise
        if not lease.matches(result.execution_echo):
            await self.leases.close_quietly(lease, "failed", end_reason="identidad_no_confirmada")
            raise IdentityUnconfirmedError(
                f"El resultado no confirma la identidad del intento {lease.attempt_id} (generación {lease.generation})"
            )
        await self.leases.transition(lease, "running", "completed", runner_confirmed=True, end_reason="completed")
        return result

    async def _supervise_lease(self, lease: Lease, process, channel: HeartbeatChannel, traza: dict) -> None:
        cfg = lease_settings()
        loop = asyncio.get_running_loop()
        last_seq = 0
        last_change = loop.time()
        entry = self.local_runners.get(lease.attempt_id)
        while process.returncode is None:
            await asyncio.sleep(cfg["renew_interval"])
            if process.returncode is not None:
                return
            seq = channel.last_seq
            now = loop.time()
            if seq > last_seq:
                last_seq = seq
                last_change = now
            elif now - last_change > cfg["heartbeat_stale"]:
                logger.warning("Intento %s: el runner dejó de latir; se detiene por su handle", lease.attempt_id)
                self.local_runners.fence(lease.attempt_id, "runner_sin_latido")
                return
            else:
                continue
            tool_events = int(sum((traza.get("eventos") or {}).get(k, 0) for k in ("tool",)))
            try:
                await self.leases.renew(lease, cfg["ttl"], last_seq, tool_events)
            except LeaseLostError:
                logger.warning("Intento %s: renovación rechazada; se detiene el runner por su handle", lease.attempt_id)
                self.local_runners.fence(lease.attempt_id, "lease_perdido")
                return
            except Exception:  # noqa: BLE001
                logger.exception("Intento %s: fallo al renovar el lease", lease.attempt_id)
            if entry is not None and entry.fenced_reason:
                return

    async def _renew_remote_lease(self, lease: Lease, traza: dict) -> None:
        cfg = lease_settings()
        seq = 0
        while True:
            await asyncio.sleep(cfg["renew_interval"])
            seq += 1
            tool_events = int((traza.get("eventos") or {}).get("tool", 0))
            try:
                await self.leases.renew(lease, cfg["ttl"], seq, tool_events)
            except LeaseLostError:
                logger.warning("Attempt %s: remote runner lease renewal rejected", lease.attempt_id)
                return
            except Exception:  # noqa: BLE001
                logger.exception("Attempt %s: remote runner lease renewal failed", lease.attempt_id)

    async def _herramientas_del_cron(self, task: Task) -> list[str]:
        from sqlalchemy import select

        schedule_id = (task.metadata_json or {}).get("scheduled_task_id")
        if not schedule_id:
            return []
        try:
            async with self.session_factory() as session:
                fila = (
                    await session.execute(
                        select(Task.tool_calls)
                        .where(
                            Task.metadata_json["scheduled_task_id"].as_string() == str(schedule_id),
                            Task.status == "completed",
                            Task.id != task.id,
                        )
                        .order_by(Task.completed_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
        except Exception:  # noqa: BLE001
            return []
        return sorted({str(c.get("name")) for c in (fila or []) if isinstance(c, dict) and c.get("name")})

    async def _run_runner(
        self,
        agent: Agent,
        task: Task,
        stream_callback,
        api_key: str | None,
        runtime_system_prompt: str,
        runtime_python_bin: str,
        conversation_history: list[dict] | None = None,
        session_id: str | None = None,
        fallback_override: dict | None = None,
        lease: Lease | None = None,
        runtime_version: str | None = None,
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
        exposicion = None
        toolsets_antes = list(enabled_toolsets or [])
        if exposicion_activa(agent):
            enabled_toolsets, pedidos, sueltas = seleccion_progresiva_detallada(
                task, enabled_toolsets, conversation_history
            )
            exposicion = {
                "toolsets_pedido": pedidos,
                "herramientas_pedido": sorted(
                    set(sueltas)
                    | set(await self._herramientas_del_cron(task))
                    | set(getattr(conversation_history, "herramientas", None) or [])
                ),
            }
        else:
            enabled_toolsets = filtrar_para_tarea(task, enabled_toolsets, conversation_history)
        toolsets_diferidos = [t for t in toolsets_antes if t not in (enabled_toolsets or [])]
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
            runtime_system_prompt + "\n\n" + REGLA_FUENTES + "\n\n" + REGLA_VERIFICACION + "\n\n" + REGLA_HERRAMIENTAS
        )
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

        runtime_system_prompt = self._with_platform_rules(runtime_system_prompt)

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
            "prompt": self._prompt_with_notices(
                self._prompt_with_previous_files(task.prompt, workspace_path, conversation_history, task.metadata_json),
                task.metadata_json,
                workspace_path,
            ),
            "system_override": task.system_override,
            "model": effective_model,
            "provider": runtime_provider,
            "base_url": effective_base_url,
            "api_key": api_key,
            "enabled_toolsets": enabled_toolsets or None,
            **({"exposicion": exposicion} if exposicion is not None else {}),
            **({"toolsets_diferidos": toolsets_diferidos} if toolsets_diferidos else {}),
            "disabled_toolsets": disabled_toolsets or None,
            "max_iterations": agent.max_iterations,
            "system_prompt": runtime_system_prompt,
            "cwd": execution_workspace,
            "workspace_guard": _workspace_isolation_enabled(),
            "guardia_accion_narrada": _guardia_accion_narrada_activa(),
            "guardia_desbocada": _guardia_desbocada(),
            "hermes_home": f"{execution_workspace}/.hermes",
            "conversation_history": conversation_history or [],
            "session_id": session_id,
            "metadata": task.metadata_json or {},
            "memoria_persistente": memoria_persistente_para(task.metadata_json),
        }
        channel: HeartbeatChannel | None = None
        if lease is not None:
            cfg_latido = lease_settings()
            payload["execution"] = lease.identity()
            if self.runtime_runner_client is None:
                channel = HeartbeatChannel(
                    lease.execution_id,
                    lease.attempt_id,
                    lease.generation,
                    max_age_seconds=cfg_latido["heartbeat_stale"],
                )
                channel.open()
                payload["execution"].update(channel.runner_config(cfg_latido["heartbeat_interval"]))
            process_env = {
                **process_env,
                "HERMESHQ_EXECUTION_ID": lease.execution_id,
                "HERMESHQ_ATTEMPT_ID": lease.attempt_id,
                "HERMESHQ_LEASE_GENERATION": str(lease.generation),
            }
            from hermeshq.services import credencial_agente

            if credencial_agente.emision_activa():
                payload["execution"]["agent_token"] = credencial_agente.emitir(
                    agent.id, task.id, lease.attempt_id, lease.generation, self._task_timeout_for(task) + 900
                )
                process_env.pop("HERMESHQ_AGENT_TOKEN", None)

        host_hermes_home = self.installation_manager.build_hermes_home(agent.workspace_path)
        request_overrides = self._build_request_overrides(agent, host_hermes_home, task)
        if request_overrides:
            payload["request_overrides"] = request_overrides
        if self._effective_thinking_mode(agent, task) == "adaptive":
            payload["thinking_adaptive"] = True

        if fallback_override:
            for key, value in fallback_override.items():
                if value is not None:
                    payload[key] = value

        task_timeout = self._task_timeout_for(task)
        for env_key, env_value in self._model_timeout_env().items():
            process_env.setdefault(env_key, env_value)

        from hermeshq.services import runner_reserva as rr

        network_policy: dict | None = None
        if self.runtime_runner_client is not None:
            policy = await PermissionEnforcer(self.session_factory).get_policy(agent)
            if policy is not None:
                rules = policy.network_rules or {}
                if rules.get("deny_all"):
                    allow = enforceable_egress_domains(rules.get("allow_domains"))
                    for host in llm_egress_hosts([effective_base_url, agent.fallback_base_url]):
                        if host not in allow:
                            allow.append(host)
                    network_policy = {"deny_all": True, "allow_domains": allow}

        reserva = rr.reserva_activa() if channel is not None else None
        clave_reserva = entorno_reserva = None
        process: asyncio.subprocess.Process | None = None
        try:
            if reserva is not None:
                entorno_reserva = rr.entorno_base(process_env)
                clave_reserva = rr.clave(str(agent.id), str(runtime_python_bin), entorno_reserva)
                en_reserva = reserva.tomar(clave_reserva)
                if en_reserva is not None:
                    channel.adoptar(en_reserva.read_fd, en_reserva.write_fd)
                    payload["execution"]["heartbeat_fd"] = en_reserva.write_fd
                    process = en_reserva.proceso
            if process is None and self.runtime_runner_client is None:
                process = await asyncio.create_subprocess_exec(
                    runtime_python_bin,
                    str(Path(__file__).resolve().parents[1] / "scripts" / "hermes_task_runner.py"),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=1024 * 1024,
                    cwd=str(Path(__file__).resolve().parents[2]),
                    env=process_env,
                    pass_fds=(channel.runner_config(0)["heartbeat_fd"],) if channel is not None else (),
                )
            if channel is not None:
                await channel.start()
            if reserva is not None and clave_reserva is not None:
                _tareas_reserva.add(
                    asyncio.create_task(reserva.preparar(clave_reserva, str(runtime_python_bin), entorno_reserva))
                )
                for hecha in [t for t in _tareas_reserva if t.done()]:
                    _tareas_reserva.discard(hecha)
        except BaseException:
            if channel is not None:
                await channel.close()
            raise

        final_result: dict | None = None
        segmentos: list[str] = [""]
        traza: dict = {"lineas": 0, "eventos": {}, "no_json": 0, "ultima_no_json": "", "mayor_linea": 0}
        identidad_runner: dict = {}
        supervisor_lease: asyncio.Task | None = None
        local_entry = None
        if lease is not None and channel is not None and process is not None:
            local_entry = self.local_runners.register(lease, process)
            supervisor_lease = asyncio.create_task(self._supervise_lease(lease, process, channel, traza))
        elif lease is not None and self.runtime_runner_client is not None:
            supervisor_lease = asyncio.create_task(self._renew_remote_lease(lease, traza))

        async def _entregar(texto: str) -> None:
            segmentos[-1] += texto
            await stream_callback(texto)

        async def _consume() -> dict | None:
            result: dict | None = None

            async def handle_line(text: str, raw_length: int | None = None) -> None:
                nonlocal result
                if not text:
                    return
                traza["lineas"] += 1
                traza["mayor_linea"] = max(traza["mayor_linea"], raw_length or len(text))
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    self._registrar_no_json(traza, text, task.id)
                    return
                tipo_evento = str(event.get("event"))
                traza["eventos"][tipo_evento] = traza["eventos"].get(tipo_evento, 0) + 1

                if event.get("event") == "delta" and event.get("data"):
                    await _entregar(str(event["data"]))
                elif event.get("event") == "segment_break":
                    if segmentos[-1].strip():
                        segmentos.append("")
                elif event.get("event") in ("tool", "step"):
                    self._accumulate_trace_event(traza, event)
                elif event.get("event") == "tool_started":
                    await self._registrar_inicio_herramienta(lease, agent, str(event.get("name") or "tool"))
                elif event.get("event") == "tool_activated":
                    await self._registrar_activacion_herramienta(lease, agent, str(event.get("name") or "tool"))
                elif event.get("event") == "tool_finished":
                    await self._registrar_fin_herramienta(
                        lease,
                        agent,
                        str(event.get("name") or "tool"),
                        str(event.get("clase") or "sin_clasificar"),
                        int(event.get("ms") or 0),
                        auditoria=event.get("auditoria") if isinstance(event.get("auditoria"), dict) else None,
                    )
                elif event.get("event") == "attempt_started":
                    if not identidad_runner and isinstance(event.get("execution"), dict):
                        identidad_runner.update(event["execution"])
                elif event.get("event") == "result":
                    result = event
                elif event.get("event") == "error":
                    traza["error_evento"] = {
                        "error_type": str(event.get("error_type") or ""),
                        "traceback": str(event.get("traceback") or ""),
                        "usage": event.get("usage") or {},
                    }
                    error = RuntimeExecutionError(str(event.get("error") or "Hermes runtime process failed"))
                    error.hq_end_reason_hint = _classify_end_reason(str(event.get("error") or ""))
                    raise error

            if self.runtime_runner_client is not None:
                async for text in self.runtime_runner_client.run(
                    engine="hermes",
                    agent_id=agent.id,
                    environment=process_env,
                    input_data=json.dumps(payload),
                    hermes_version=runtime_version,
                    network_policy=network_policy,
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
                await handle_line(line.decode("utf-8", errors="replace").strip(), len(line))
            return result

        consume_error: Exception | None = None
        end_reason: str | None = None
        try:
            final_result = await asyncio.wait_for(_consume(), timeout=task_timeout)
        except TimeoutError as exc:
            consume_error = RuntimeExecutionError(f"Hermes runtime exceeded the task timeout of {task_timeout}s")
            consume_error.__cause__ = exc
            end_reason = "timeout"
        except RuntimeExecutionError as exc:
            consume_error = exc
            end_reason = getattr(exc, "hq_end_reason_hint", None)
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    logger.warning("Task runner subprocess %s did not exit after kill", process.pid)
            if supervisor_lease is not None:
                supervisor_lease.cancel()
                try:
                    await supervisor_lease
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if lease is not None:
                self.local_runners.unregister(lease.attempt_id)
            if channel is not None:
                traza["latidos"] = {"aceptados": channel.accepted, "rechazados": channel.rejected}
                await channel.close()

        fenced_reason = local_entry.fenced_reason if local_entry is not None else None
        if fenced_reason in ("lease_perdido", "sin_titularidad"):
            raise LeaseLostError(f"Attempt {lease.attempt_id} lost its lease ({fenced_reason}); result discarded")
        if fenced_reason == "runner_sin_latido":
            error = RuntimeExecutionError("The runner stopped sending heartbeats and was stopped")
            error.hq_end_reason_hint = "runner_sin_latido"
            _attach_partial(error, traza, "runtime_error", "", api_key)
            raise error

        stderr_output = ""
        if process is not None and process.stderr is not None:
            try:
                stderr_output = (
                    (await asyncio.wait_for(process.stderr.read(), timeout=10))
                    .decode("utf-8", errors="replace")
                    .strip()
                )
            except TimeoutError:
                stderr_output = ""

        if consume_error is not None:
            _attach_partial(consume_error, traza, end_reason, stderr_output, api_key)
            raise consume_error

        return_code: int | None = None
        if process is not None:
            return_code = process.returncode if process.returncode is not None else await process.wait()
        if process is not None and return_code != 0 and not final_result:
            error = RuntimeExecutionError(stderr_output or "Hermes runtime process exited with an error")
            _attach_partial(error, traza, "runtime_error", stderr_output, api_key)
            raise error
        if not final_result:
            logger.warning(
                "Hermes runtime ended without a result payload (task=%s, returncode=%s, trace=%s, stderr=%s)",
                task.id,
                return_code,
                json.dumps(traza, ensure_ascii=False),
                stderr_output[-1500:] or "<empty>",
            )
            final_result = self._recover_result_from_stream(segmentos, return_code)
            if final_result and identidad_runner:
                final_result["execution"] = dict(identidad_runner)
            if not final_result:
                error = RuntimeExecutionError("Hermes runtime returned no result payload")
                _attach_partial(error, traza, "runtime_error", stderr_output, api_key)
                raise error
            logger.warning("Recovered the final answer of task %s from the streamed text", task.id)

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
        isolated_root = _Path(execution_workspace)
        from hermeshq.services.formato_pedido import extensiones_pedidas

        _extensiones_pedidas = extensiones_pedidas(task.prompt)
        _carpetas_generadas = {(workspace_path / c).resolve() for c in ("outputs", "output")}
        _collected_by_source: dict[str, dict] = {}
        _adjuntos_rechazados: dict[str, str] = {}
        _avisos_de_adjuntos: list[str] = []

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
            nombre_pedido = _re.split(r"[\\/]", file_path_str.strip())[-1] or file_path_str
            if not file_path.exists() or not file_path.is_file():
                logger.warning("%s: file not found: %s", log_prefix, file_path_str)
                if log_prefix == "MEDIA":
                    _adjuntos_rechazados[f"no-existe:{file_path_str}"] = (
                        f"⚠️ No pude adjuntar «{nombre_pedido}»: el archivo no existe, así que no se generó o se "
                        "guardó en otra ruta. Pídemelo de nuevo."
                    )
                return None
            if not media_path_allowed(file_path, workspace_path, _shared_media_roots()):
                logger.warning(
                    "%s: ruta fuera del workspace del agente, adjunto descartado: %s", log_prefix, file_path_str
                )
                if log_prefix == "MEDIA" and not ruta_interna_de_skills(file_path_str):
                    _adjuntos_rechazados[f"fuera:{file_path_str}"] = (
                        f"⚠️ No pude adjuntar «{nombre_pedido}»: está fuera de mi espacio de trabajo."
                    )
                return None
            if extraccion_interna(file_path_str) or extraccion_interna(file_path):
                logger.warning("%s: extracción interna de datos, adjunto descartado: %s", log_prefix, file_path_str)
                return None
            resolved = str(file_path.resolve())
            if resolved in _collected_by_source:
                return _collected_by_source[resolved]

            from hermeshq.services import validar_adjunto

            ext = file_path.suffix.lower()
            if ext and ext not in _ALLOWED_MEDIA_EXTS:
                logger.warning("%s: file extension not allowed: %s", log_prefix, ext)
                return None
            if (
                _extensiones_pedidas is not None
                and ext not in _extensiones_pedidas
                and file_path.resolve().parent in _carpetas_generadas
            ):
                logger.info("%s: %s no es el formato pedido; no se adjunta", log_prefix, file_path.name)
                return None
            revision = validar_adjunto.validar(file_path)
            if not revision.valido:
                logger.warning("%s: adjunto inválido %s: %s", log_prefix, file_path.name, revision.motivo)
                _adjuntos_rechazados[resolved] = (
                    f"⚠️ No adjunté {file_path.name}: {revision.motivo}. Lo genero de nuevo si me lo pides."
                )
                return None
            nombre_entregado = file_path.name
            if not ext:
                ext = revision.extension
                nombre_entregado = f"{file_path.name}{ext}"
                if ext not in _ALLOWED_MEDIA_EXTS:
                    return None
            _avisos_de_adjuntos.extend(revision.avisos)

            uploads_dir.mkdir(parents=True, exist_ok=True)
            file_id = str(_uuid.uuid4())
            dest_path = uploads_dir / f"{file_id}{ext}"
            try:
                _shutil.copy2(file_path, dest_path)
            except OSError as exc:
                logger.warning("%s: failed to copy %s: %s", log_prefix, file_path, exc)
                return None
            registrar_nombre_original(uploads_dir, dest_path.name, nombre_entregado)

            attachment = {
                "file_id": file_id,
                "filename": nombre_entregado,
                "media_type": _MEDIA_EXT_MIME_MAP.get(ext, "application/octet-stream"),
                "size": dest_path.stat().st_size,
                "caption": "",
                "source_path": str(file_path),
            }
            _collected_by_source[resolved] = attachment
            logger.info("%s: collected %s (%d bytes) as %s", log_prefix, file_path.name, attachment["size"], file_id)
            return attachment

        raw_response = _re.sub(r"(?im)^[ \t*_`]*MEDIA:[ \t]*(https?://\S+?)[ \t*_`]*$", r"\1", raw_response)
        media_pattern = _re.compile(r"(?im)^[ \t*_`]*MEDIA:[ \t]*(\S.*?)[ \t*_`]*$")
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
            "hq_pptx_editar",
            "hq_xlsx_generate",
            "hq_xlsx_editar",
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
            generados = []
            for msg in task_messages:
                if msg.get("role") != "tool" or msg.get("tool_call_id") not in _auto_attach_call_ids:
                    continue
                try:
                    tool_result = _json.loads(msg.get("content") or "{}")
                except (ValueError, TypeError):
                    continue
                result_path = tool_result.get("path") if isinstance(tool_result, dict) else None
                if tool_result.get("success") and result_path:
                    generados.append(str(result_path))
            for result_path in ultimas_versiones(generados):
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
        avisos = list(_adjuntos_rechazados.values()) + list(dict.fromkeys(_avisos_de_adjuntos))
        if avisos:
            raw_response = "\n\n".join([raw_response, *avisos]) if raw_response else "\n\n".join(avisos)
        if _collected_by_source:
            markers = "\n".join(
                f"MEDIA: uploads/{att['file_id']}{_Path(att['filename']).suffix.lower()}"
                for att in _collected_by_source.values()
            )
            raw_response = f"{raw_response}\n\n{markers}" if raw_response else markers

        # Merge with any attachments the task runner already collected
        runner_attachments = list(final_result.get("response_attachments") or [])
        all_attachments = runner_attachments + list(_collected_by_source.values())

        usage = final_result.get("usage")
        return RuntimeExecutionResult(
            final_response=raw_response,
            messages=list(final_result.get("messages") or []),
            tool_calls=list(final_result.get("tool_calls") or []),
            tokens_used=int(final_result.get("tokens_used") or 0),
            iterations=int(final_result.get("iterations") or 0),
            engine=str(final_result.get("engine") or "hermes-agent"),
            response_attachments=all_attachments,
            usage=dict(usage) if isinstance(usage, dict) else None,
            execution_echo=final_result.get("execution") if isinstance(final_result.get("execution"), dict) else None,
            exposicion=final_result.get("exposicion") if isinstance(final_result.get("exposicion"), dict) else None,
            compactacion=final_result.get("compactacion")
            if isinstance(final_result.get("compactacion"), dict)
            else None,
            accion_narrada=final_result.get("accion_narrada")
            if isinstance(final_result.get("accion_narrada"), dict)
            else None,
            generacion_desbocada=final_result.get("generacion_desbocada")
            if isinstance(final_result.get("generacion_desbocada"), dict)
            else None,
            verificacion=final_result.get("verificacion")
            if isinstance(final_result.get("verificacion"), dict)
            else None,
            correcciones_aplicadas=final_result.get("correcciones_aplicadas")
            if isinstance(final_result.get("correcciones_aplicadas"), int)
            else None,
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
