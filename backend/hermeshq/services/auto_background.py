import logging
import re

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.core.security import can_access_agent
from hermeshq.models.activity import ActivityLog
from hermeshq.models.public_chat import PublicChatMessage
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services.archivos_conversacion import CLAVE_SESION_ORIGEN, ENCABEZADO, con_archivos_generados
from hermeshq.services.background_style import SIN_TEXTO_ENTRE_HERRAMIENTAS
from hermeshq.services.cancel_intent import is_ack_request, is_reset_request, is_status_request
from hermeshq.services.confirmacion import confirma_una_aprobacion
from hermeshq.services.cron_schedule import DEFAULT_TIMEZONE, current_time_context
from hermeshq.services.respuesta_corta import pedido_original
from hermeshq.services.schedule_notifications import metadata_de_entrega
from hermeshq.services.task_board import next_board_order, runtime_status_to_board_column

logger = logging.getLogger(__name__)

TIMEOUT_MARKER = "exceeded the task timeout"
SIN_SALIDA_MARKER = "produced no output for"
INCOMPLETO_MARKER = "resultado incompleto:"
MOTIVOS_INCOMPLETOS = ("max_iterations", "guardrail")
HISTORY_MESSAGES = 2
HISTORY_CHARS_PER_MESSAGE = 1200
MAX_TITLE_CHARS = 120
AUTO_MARKER = "auto_background_job_id"


def motivo_resultado_incompleto(usage) -> str | None:
    razon = str((usage or {}).get("turn_exit_reason") or "") if isinstance(usage, dict) else ""
    return next((motivo for motivo in MOTIVOS_INCOMPLETOS if razon.startswith(motivo)), None)


def error_de_incompleto(motivo: str) -> str:
    return f"{INCOMPLETO_MARKER} {motivo}"


def _corte_sin_salida(error: str) -> bool:
    texto = error or ""
    return SIN_SALIDA_MARKER in texto and TIMEOUT_MARKER not in texto


def _corte_admitido(error: str, sin_acciones_externas: bool, incompleto: bool = False) -> bool:
    texto = error or ""
    if TIMEOUT_MARKER in texto or (incompleto and INCOMPLETO_MARKER in texto):
        return True
    return sin_acciones_externas and SIN_SALIDA_MARKER in texto


async def sin_acciones_externas(session: AsyncSession, task) -> bool:
    if (getattr(task, "metadata_json", None) or {}).get("acciones_externas"):
        return False
    fila = await session.execute(
        text(
            "SELECT count(*) FROM external_action_records WHERE task_id = :t "
            "AND state NOT IN ('no_ejecutada', 'conciliada_no_ejecutada')"
        ),
        {"t": task.id},
    )
    return int(fila.scalar() or 0) == 0


async def _sin_efectos_si_hace_falta(session: AsyncSession, task, error: str) -> bool:
    if not _corte_sin_salida(error):
        return True
    return await sin_acciones_externas(session, task)


def is_continuable(task, error: str, sin_acciones_externas: bool = False) -> bool:
    metadata = getattr(task, "metadata_json", None) or {}
    if metadata.get("source") != "public_chat" or metadata.get("background_job") or metadata.get("scheduled"):
        return False
    if not metadata.get("thread_user_id") or metadata.get("public_chat_binding_denied"):
        return False
    return _corte_admitido(error, sin_acciones_externas) and bool((getattr(task, "prompt", "") or "").strip())


ATTACHMENT_NOTE = re.compile(r"^\s*\[Archivos adjuntos.*?\]\s*", re.DOTALL)


def _title_from(prompt: str) -> str:
    text = ATTACHMENT_NOTE.sub("", pedido_original(prompt), count=1).strip()
    return _clip(text or "solicitud con archivos adjuntos", MAX_TITLE_CHARS)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


RESET_REPLY_PREFIX = "🧹"
SYSTEM_REPLY_PREFIXES = ("⚠️ Mi intento anterior", "⏳", "⏹️", RESET_REPLY_PREFIX)
LOOKBACK_MESSAGES = 40
SUBSTANTIAL_REQUEST_CHARS = 80
REQUEST_THEME_MIN_CHARS = 25


def _is_system_reply(message) -> bool:
    return message.role != "user" and (message.content or "").lstrip().startswith(SYSTEM_REPLY_PREFIXES)


def _is_reset_marker(message) -> bool:
    content = message.content or ""
    if getattr(message, "role", None) == "user":
        return is_reset_request(content)
    return content.lstrip().startswith(RESET_REPLY_PREFIX)


def _looks_like_request(text: str | None) -> bool:
    cleaned = ATTACHMENT_NOTE.sub("", text or "", count=1).strip()
    if len(cleaned) < REQUEST_THEME_MIN_CHARS:
        return False
    return not (is_ack_request(cleaned) or is_status_request(cleaned) or is_reset_request(cleaned))


async def _load_recent_messages(session: AsyncSession, session_id: str | None) -> list:
    if not session_id:
        return []
    result = await session.execute(
        select(PublicChatMessage)
        .where(PublicChatMessage.session_id == session_id)
        .order_by(PublicChatMessage.created_at.desc())
        .limit(LOOKBACK_MESSAGES)
    )
    after_reset = []
    for message in result.scalars().all():
        if _is_reset_marker(message):
            break
        after_reset.append(message)
    return [m for m in after_reset if not _is_system_reply(m)]


def _history_text(newest_first: list) -> str:
    recent = newest_first[:HISTORY_MESSAGES]
    lines = []
    for message in reversed(recent):
        who = "Usuario" if message.role == "user" else "Tú"
        lines.append(f"{who}: {_clip(message.content, HISTORY_CHARS_PER_MESSAGE)}")
    return "\n".join(lines)


def _has_theme(newest_first: list, prompt: str | None) -> bool:
    if any(m.role == "user" and _looks_like_request(m.content) for m in newest_first):
        return True
    return _looks_like_request(prompt)


async def _recent_history(session: AsyncSession, session_id: str | None) -> str:
    if not session_id:
        return ""
    return _history_text(await _load_recent_messages(session, session_id))


async def create_continuation(session: AsyncSession, task, error: str) -> Task | None:
    if not is_continuable(task, error, sin_acciones_externas=True):
        return None
    if not await _sin_efectos_si_hace_falta(session, task, error):
        return None
    metadata = task.metadata_json or {}
    user_id = str(metadata["thread_user_id"])
    user = await session.get(User, user_id)
    if not user or not user.is_active or not await can_access_agent(session, user, task.agent_id):
        return None
    active = await session.execute(
        select(Task).where(Task.agent_id == task.agent_id, Task.status.in_(("queued", "running")))
    )
    for item in active.scalars().all():
        item_metadata = item.metadata_json or {}
        if item_metadata.get("background_job") and item_metadata.get("thread_user_id") == user_id:
            return None
    timeout_minutes = int(get_settings().background_job_timeout_seconds) // 60
    title = _title_from(task.prompt)
    mensajes = await _load_recent_messages(session, metadata.get("public_session_id"))
    if confirma_una_aprobacion(task, mensajes):
        return None
    tiempo = current_time_context(DEFAULT_TIMEZONE)
    if _has_theme(mensajes, task.prompt):
        history = _history_text(mensajes)
        context = f"Conversación reciente:\n{history}\n\n" if history else ""
        if ENCABEZADO not in (task.prompt or ""):
            context = await con_archivos_generados(
                session,
                task.agent_id,
                user_id,
                str(metadata.get("public_session_id") or ""),
                metadata.get("public_chat_api_key_id"),
                context,
            )
        prompt = (
            f"{context}Último mensaje del usuario, que debes resolver:\n{task.prompt.strip()}\n\n"
            "[CONTINUACIÓN AUTOMÁTICA EN SEGUNDO PLANO: tu intento anterior en el chat agotó el tiempo y se cortó. "
            "El usuario no ve este trabajo ni puedes preguntarle nada. Antes de empezar revisa tu workspace "
            "(work/, sharepoint_downloads/) y reutiliza solo los archivos que correspondan al tema de esta "
            "solicitud: no repitas descargas ni extracciones, pero ignora cualquier archivo de un tema distinto "
            "que haya quedado de antes. Usa las herramientas del catálogo de capacidades en vez de escribir "
            "scripts a medida. "
            + SIN_TEXTO_ENTRE_HERRAMIENTAS
            + " Entrega al final el resultado COMPLETO y listo para leer (conclusión primero, "
            "cifras exactas de la fuente, sin inventar). Si generas un archivo, súbelo a OneDrive con "
            f"sharepoint_upload_file y da la ruta. Tienes hasta {timeout_minutes} minutos.]\n\n{tiempo}"
        )
    else:
        prompt = (
            "[CONTINUACIÓN AUTOMÁTICA EN SEGUNDO PLANO: tu intento anterior en el chat se cortó por falta de "
            "tiempo, pero la conversación se reinició y no quedó claro qué necesitaba el usuario. NO generes "
            "ningún archivo ni uses los que quedaron en el workspace, y NO inventes el pedido. Responde "
            "únicamente con un aviso breve y amable en español que diga que no alcanzaste a terminar y que, para "
            f"retomarlo, vuelva a indicar en un mensaje qué necesita (tema y formato).]\n\n{tiempo}"
        )
    job = Task(
        agent_id=task.agent_id,
        created_by_user_id=None,
        title=f"Trabajo en segundo plano: {title}"[:255],
        prompt=prompt,
        metadata_json={
            "background_job": True,
            "background_title": title,
            "source": "background_job",
            "thread_user_id": user_id,
            "origin_task_id": task.id,
            "mensaje_usuario": str(metadata.get("mensaje_usuario") or pedido_original(task.prompt or ""))[:2000],
            "auto_continuation": True,
            **({CLAVE_SESION_ORIGEN: str(metadata["public_session_id"])} if metadata.get("public_session_id") else {}),
            **metadata_de_entrega(metadata),
        },
    )
    job.board_column = runtime_status_to_board_column(job.status)
    job.board_order = next_board_order()
    job.board_manual = False
    session.add(job)
    await session.flush()
    task.metadata_json = {**metadata, AUTO_MARKER: job.id}
    session.add(
        ActivityLog(
            agent_id=task.agent_id,
            task_id=task.id,
            event_type="background_job.auto_started",
            message=title,
            details={
                "requested_for_user_id": user_id,
                "reason": "chat_sin_salida" if _corte_sin_salida(error) else "chat_timeout",
            },
        )
    )
    return job


MAX_CONTINUACIONES_BACKGROUND = 2


def is_background_continuable(task, error: str, sin_acciones_externas: bool = False) -> bool:
    metadata = getattr(task, "metadata_json", None) or {}
    if not metadata.get("background_job") or metadata.get("public_chat_binding_denied"):
        return False
    if (
        not metadata.get("thread_user_id")
        or int(metadata.get("continuaciones", 0) or 0) >= MAX_CONTINUACIONES_BACKGROUND
    ):
        return False
    return _corte_admitido(error, sin_acciones_externas, incompleto=True) and bool(
        (getattr(task, "prompt", "") or "").strip()
    )


def _motivo_del_corte(error: str) -> str:
    texto = error or ""
    if INCOMPLETO_MARKER in texto:
        motivo = texto.split(INCOMPLETO_MARKER, 1)[1].strip()
        if motivo == "guardrail":
            return (
                "se detuvo porque una herramienta falló varias veces seguidas; no insistas con la misma llamada, "
                "usa otra estrategia o herramienta"
            )
        return "llegó al límite de pasos permitidos; avanza con menos pasos y no repitas lecturas ya hechas"
    if _corte_sin_salida(texto):
        return "se quedó sin responder y se detuvo"
    return "llegó al límite de tiempo"


def prompt_continuacion_background(original: str, parte: int, minutos: int, error: str = "") -> str:
    return (
        f"[CONTINUACIÓN AUTOMÁTICA DE TRABAJO EN SEGUNDO PLANO (parte {parte}): la corrida anterior "
        f"{_motivo_del_corte(error)}. Revisa tu workspace (work/, sharepoint_downloads/) y lo que ya quedó generado; "
        "no repitas lo terminado, continúa lo pendiente y entrega el resultado COMPLETO en español. "
        f"{SIN_TEXTO_ENTRE_HERRAMIENTAS} Tienes hasta {minutos} minutos.]\n\n{pedido_original(original)}"
    )


async def create_background_continuation(session: AsyncSession, task, error: str) -> Task | None:
    if not is_background_continuable(task, error, sin_acciones_externas=True):
        return None
    if not await _sin_efectos_si_hace_falta(session, task, error):
        return None
    metadata = task.metadata_json or {}
    user_id = str(metadata["thread_user_id"])
    user = await session.get(User, user_id)
    if not user or not user.is_active or not await can_access_agent(session, user, task.agent_id):
        return None
    parte = int(metadata.get("continuaciones", 0) or 0) + 1
    titulo = str(metadata.get("background_title") or _title_from(task.prompt))
    minutos = int(get_settings().background_job_timeout_seconds) // 60
    job = Task(
        agent_id=task.agent_id,
        created_by_user_id=None,
        title=f"Trabajo en segundo plano: {titulo} (parte {parte})"[:255],
        prompt=prompt_continuacion_background(task.prompt, parte, minutos, error),
        metadata_json={
            "background_job": True,
            "background_title": titulo,
            "source": "background_job",
            "thread_user_id": user_id,
            "origin_task_id": task.id,
            "continuaciones": parte,
            **metadata_de_entrega(metadata),
        },
    )
    job.board_column = runtime_status_to_board_column(job.status)
    job.board_order = next_board_order()
    job.board_manual = False
    session.add(job)
    await session.flush()
    task.metadata_json = {**metadata, AUTO_MARKER: job.id}
    session.add(
        ActivityLog(
            agent_id=task.agent_id,
            task_id=task.id,
            event_type="background_job.continued",
            message=titulo,
            details={"requested_for_user_id": user_id, "parte": parte, "motivo": _motivo_del_corte(error)},
        )
    )
    return job
