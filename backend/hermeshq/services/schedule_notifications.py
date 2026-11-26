import json
import logging
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification, PublicChatSession
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.services.confirmacion import es_confirmacion
from hermeshq.services.delegacion_entre_usuarios import es_cuenta_de_servicio
from hermeshq.services.texto_chat import LINEA_MEDIA, quitar_lineas_media, tablas_para_teams

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 12000
MAX_FAILURE_CHARS = 1000
NOTIFICATION_RETENTION = timedelta(days=7)
NOTIFICATION_DELIVERY_WINDOW = timedelta(hours=12)
BRIDGE_LIVENESS_WINDOW = timedelta(hours=12)
CLAVES_DE_ENTREGA = ("public_chat_api_key_id", "bot_channel_id")


def metadata_de_entrega(origen: dict | None) -> dict:
    origen = origen or {}
    return {
        campo: str(origen[campo]) for campo in CLAVES_DE_ENTREGA if isinstance(origen.get(campo), str) and origen[campo]
    }


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n\n…(resultado recortado, ver HermesHQ)"


def friendly_error_text(raw_error: str | None) -> str:
    raw = str(raw_error or "")
    if "exceeded the task timeout" in raw or "produced no output" in raw:
        return "Tu solicitud tardó demasiado y se detuvo. Intenta con una petición más acotada o con menos archivos."
    if "empty_response_exhausted" in raw:
        return (
            "El modelo no devolvió respuesta (tres intentos vacíos) y tu solicitud no se procesó; no se ejecutó nada. "
            "Vuelve a enviar el mensaje; si se repite, avísale al administrador."
        )
    if "invalid tool call" in raw or "stopped early" in raw:
        return (
            "El agente generó una llamada inválida y se detuvo. Reintenta el mensaje; "
            "si pedías un informe grande, pídelo por partes (de a 2 capítulos)."
        )
    if "sequence item" in raw or "expected str instance" in raw:
        return (
            "No se pudo procesar tu solicitud por un error interno. Reinténtalo; si persiste, avísale al administrador."
        )
    return "Hubo un problema al procesar tu solicitud. Reinténtalo en unos minutos; si persiste, acótala o pídela por partes."


_RUNTIME_ERROR_MARKERS = (
    "exceeded the task timeout",
    "produced no output",
    "invalid tool call",
    "stopped early",
    "sequence item",
    "expected str instance",
)


CORTE_DE_STREAM = "Stream stalled mid tool-call"
TEXTO_CORTE_DE_STREAM = (
    "se cortó la conexión con el modelo a mitad de una acción, así que esa acción no se realizó. "
    "Escribe «reintenta» para volver a intentarlo."
)


DESCRIPCION_INCOMPLETO = {
    "max_iterations": "llegó al límite de pasos permitidos",
    "guardrail": "se detuvo porque una herramienta falló varias veces seguidas",
}


def motivo_incompleto(task) -> str | None:
    metadata = getattr(task, "metadata_json", None) or {}
    if not metadata.get("resultado_incompleto"):
        return None
    return DESCRIPCION_INCOMPLETO.get(str(metadata.get("end_reason") or ""), "se detuvo antes de terminar")


_ADMITE_FALLO = re.compile(
    r"^\W*(lamentablemente,?\s+)?no\s+(pude|se pudo|logr[ée]|fue posible|he podido|alcanc[ée]|tengo acceso|"
    r"se (gener[óo]|envi[óo]|cre[óo]|complet[óo]|subi[óo]|comparti[óo]))\b"
    r"|^\s*(⚠️?|❌)?\s*detuve esta respuesta\b|^\s*(⚠️?|❌)\s*no (pude|alcanc)",
    re.IGNORECASE,
)


def admite_que_no_pudo(summary: str | None) -> bool:
    return bool(summary and _ADMITE_FALLO.search(summary.strip()[:300]))


_PREFIJOS_DE_DATOS = ("ms365_", "sharepoint_", "teams_")
_SIN_LECTURA_REAL = {"ms365_correo_sync": ("ventana_leida", "tablas_md", "repetido")}


def datos_no_obtenidos(task) -> bool:
    mensajes = getattr(task, "messages_json", None)
    if not isinstance(mensajes, list):
        return False
    fallos = 0
    for mensaje in mensajes:
        if not isinstance(mensaje, dict) or mensaje.get("role") != "tool":
            continue
        nombre = str(mensaje.get("name") or "")
        if not nombre.startswith(_PREFIJOS_DE_DATOS):
            continue
        try:
            resultado = json.loads(mensaje.get("content") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(resultado, dict):
            continue
        if resultado.get("success") is True:
            claves = _SIN_LECTURA_REAL.get(nombre)
            if not claves or any(k in resultado for k in claves):
                return False
        elif resultado.get("success") is False:
            fallos += 1
    return fallos > 0


def _resumen_de_background(success: bool, summary: str | None) -> tuple[bool, str | None]:
    if summary and CORTE_DE_STREAM in summary:
        return False, TEXTO_CORTE_DE_STREAM
    return success, summary


def _con_texto_si_solo_adjuntos(summary: str | None) -> str | None:
    if not summary or quitar_lineas_media(summary):
        return summary
    n = len(LINEA_MEDIA.findall(summary))
    return summary + ("\n\nTe dejo el archivo adjunto." if n == 1 else f"\n\nTe dejo los {n} archivos adjuntos.")


def _failure_body_detail(summary: str | None) -> str:
    if not summary:
        return "error desconocido"
    if any(marker in summary for marker in _RUNTIME_ERROR_MARKERS):
        return friendly_error_text(summary)
    return summary


async def _contexto_de_canales(session: AsyncSession, user) -> dict:
    from hermeshq.services.canales_bot import canal_por_defecto, identidades_de_usuario

    try:
        defecto = await canal_por_defecto(session)
        identidades = await identidades_de_usuario(session, user.id)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudieron leer los canales de bot del usuario %s", getattr(user, "id", None))
        return {"defecto": None, "identidades": {}}
    return {
        "defecto": str(defecto.id) if defecto else None,
        "identidades": {str(i.bot_channel_id): i for i in identidades},
    }


def _canal_de(key, contexto: dict) -> str | None:
    return str(getattr(key, "bot_channel_id", None) or "") or contexto.get("defecto")


def _es_canal_por_defecto(key, contexto: dict) -> bool:
    canal = getattr(key, "bot_channel_id", None)
    return not canal or str(canal) == str(contexto.get("defecto") or "")


def _identidad_de(key, contexto: dict):
    canal = _canal_de(key, contexto)
    return contexto["identidades"].get(canal) if canal else None


def _remitente(key, user, contexto: dict) -> str | None:
    if not getattr(key, "trust_sender_id", False):
        return None
    identidad = _identidad_de(key, contexto)
    if identidad is not None and identidad.external_user_id:
        return identidad.external_user_id
    return user.teams_id if _es_canal_por_defecto(key, contexto) else None


async def _claves_activas(session: AsyncSession, user, agent_id: str) -> list:
    result = await session.execute(
        select(PublicChatApiKey).where(
            PublicChatApiKey.agent_id == agent_id,
            PublicChatApiKey.is_active.is_(True),
            or_(
                PublicChatApiKey.bound_user_id == user.id,
                and_(PublicChatApiKey.trust_sender_id.is_(True), PublicChatApiKey.bound_user_id.is_(None)),
            ),
        )
    )
    claves = list(result.scalars().all())
    if not any(_es_de_confianza(k) for k in claves):
        return claves
    contexto = await _contexto_de_canales(session, user)
    return [key for key in claves if not _es_de_confianza(key) or _remitente(key, user, contexto)]


def _es_de_confianza(key) -> bool:
    return bool(getattr(key, "trust_sender_id", False)) and getattr(key, "bound_user_id", None) is None


async def _delivery_keys(session: AsyncSession, user, agent_id: str) -> list:
    return [key for key in await _claves_activas(session, user, agent_id) if _clave_viva(key)]


async def _todas_las_claves(session: AsyncSession, user, agent_id: str) -> list:
    return await _claves_activas(session, user, agent_id)


async def _preferencia(session: AsyncSession, user, agent_id: str, campo: str):
    from hermeshq.models.bot_channel import PublicChatDeliveryPref

    resultado = await session.execute(
        select(PublicChatDeliveryPref).where(
            PublicChatDeliveryPref.user_id == user.id, PublicChatDeliveryPref.agent_id == agent_id
        )
    )
    filas = [f for f in resultado.scalars().all() if isinstance(f, PublicChatDeliveryPref)]
    clave_id = getattr(filas[0], campo, None) if filas else None
    if not clave_id:
        return None
    key = await session.get(PublicChatApiKey, clave_id)
    return key if _clave_sirve_al_usuario(key, user, agent_id) else None


async def _clave_elegida_del_cron(session: AsyncSession, schedule, user, agent_id: str):
    clave_id = getattr(schedule, "delivery_key_id", None)
    if not clave_id:
        return None
    key = await session.get(PublicChatApiKey, clave_id)
    if _clave_sirve_al_usuario(key, user, agent_id) and str(getattr(schedule, "owner_user_id", "")) == str(user.id):
        return key
    logger.warning("Cron %s: delivery_key_id %s no es válida; se usa la regla de respaldo", schedule.id, clave_id)
    return None


def _clave_viva(key) -> bool:
    ultimo = getattr(key, "last_notification_poll_at", None)
    return bool(ultimo and datetime.now(UTC) - ultimo <= BRIDGE_LIVENESS_WINDOW)


def _clave_sirve_al_usuario(key, user, agent_id: str) -> bool:
    if not isinstance(key, PublicChatApiKey) or not key.is_active or str(key.agent_id) != str(agent_id):
        return False
    if key.bound_user_id:
        return str(key.bound_user_id) == str(user.id)
    return bool(key.trust_sender_id)


async def _id_clave_de_metadata(session: AsyncSession, metadata: dict) -> str | None:
    clave_id = str(metadata.get("public_chat_api_key_id") or "").strip()
    if clave_id:
        return clave_id
    session_id = str(metadata.get("public_session_id") or "").strip()
    if session_id:
        sesion = await session.get(PublicChatSession, session_id)
        if isinstance(sesion, PublicChatSession) and sesion.api_key_id:
            return str(sesion.api_key_id)
    return None


async def clave_de_origen(session: AsyncSession, task, user, agent_id: str):
    metadata = getattr(task, "metadata_json", None) or {}
    clave_id = await _id_clave_de_metadata(session, metadata)
    origen_id = str(metadata.get("origin_task_id") or "").strip()
    if not clave_id and origen_id:
        origen = await session.get(Task, origen_id)
        if isinstance(origen, Task) and str(origen.agent_id) == str(agent_id):
            clave_id = await _id_clave_de_metadata(session, origen.metadata_json or {})
    if not clave_id:
        return None
    key = await session.get(PublicChatApiKey, clave_id)
    return key if _clave_sirve_al_usuario(key, user, agent_id) else None


ORIGENES_DE_CHAT = ("respuesta_tardia", "progreso", "traspaso")


async def resolver_claves_entrega(
    session: AsyncSession, user, agent_id: str, origen: str, *, task=None, schedule=None
) -> list:
    if origen in ORIGENES_DE_CHAT or origen == "segundo_plano":
        propia = await clave_de_origen(session, task, user, agent_id) if task is not None else None
        if propia is not None:
            if origen == "segundo_plano" or _clave_viva(propia):
                return [propia]
            return []
        keys = await _delivery_keys(session, user, agent_id)
        if not keys and origen == "segundo_plano":
            keys = await _todas_las_claves(session, user, agent_id)
        return keys
    if origen == "cron":
        elegida = await _clave_elegida_del_cron(session, schedule, user, agent_id) if schedule is not None else None
        elegida = elegida or await _preferencia(session, user, agent_id, "cron_key_id")
        if elegida is not None:
            return [elegida]
        keys = await _delivery_keys(session, user, agent_id)
        if len(keys) > 1:
            logger.warning(
                "notif_multiclave_sin_preferencia: cron %s de %s se entrega en %d claves",
                getattr(schedule, "id", None),
                getattr(user, "id", None),
                len(keys),
            )
        if keys:
            return keys
        return await _todas_las_claves(session, user, agent_id)
    if origen == "delegacion":
        elegida = await _preferencia(session, user, agent_id, "notice_key_id")
        if elegida is not None:
            return [elegida]
    return await _delivery_keys(session, user, agent_id)


async def _notificaciones(session: AsyncSession, keys: list, user, origen: str, **campos) -> int:
    contexto = await _contexto_de_canales(session, user) if keys else {"defecto": None, "identidades": {}}
    for key in keys:
        identidad = _identidad_de(key, contexto)
        session.add(
            PublicChatNotification(
                api_key_id=key.id,
                recipient_user_id=user.id,
                sender_id=_remitente(key, user, contexto),
                bot_channel_id=getattr(key, "bot_channel_id", None),
                recipient_external_id=getattr(identidad, "external_user_id", None),
                recipient_aad_object_id=getattr(identidad, "aad_object_id", None),
                origin=origen,
                **campos,
            )
        )
    return len(keys)


async def enqueue_respuesta_tardia(session: AsyncSession, task, *, success: bool, summary: str | None) -> int:
    metadata = task.metadata_json or {}
    if not metadata.get("respuesta_tardia"):
        return 0
    user_id = str(metadata.get("thread_user_id") or "").strip()
    if not user_id:
        return 0
    try:
        user = await session.get(User, user_id)
        if not user or not user.is_active:
            return 0
        keys = await resolver_claves_entrega(session, user, task.agent_id, "respuesta_tardia", task=task)
        incompleto = motivo_incompleto(task) if success else None
        if incompleto:
            body = f"⚠️ No alcancé a terminar tu solicitud: {incompleto}. Esto es lo que alcancé:\n\n" + (
                _clip(tablas_para_teams(quitar_lineas_media(summary or "")), MAX_BODY_CHARS) or "(sin contenido)"
            )
        elif success:
            body = (
                _clip(tablas_para_teams(quitar_lineas_media(summary or "")), MAX_BODY_CHARS)
                or "(terminó sin contenido)"
            )
        elif es_confirmacion(task):
            body = (
                "No alcancé a completar tu confirmación a tiempo. Escribe «sí» de nuevo: si ya se había hecho, te lo "
                "digo y no se repite."
            )
        else:
            detalle = _failure_body_detail(summary)
            body = "No pude completar tu solicitud: " + _clip(detalle, MAX_FAILURE_CHARS)
        return await _notificaciones(
            session,
            keys,
            user,
            "respuesta_tardia",
            agent_id=task.agent_id,
            schedule_id=None,
            task_id=task.id,
            title="Respuesta a tu solicitud",
            body=body,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to enqueue late reply notification for task %s", getattr(task, "id", None))
        return 0


async def enqueue_progress_notice(session: AsyncSession, task, body: str) -> int:
    metadata = task.metadata_json or {}
    user_id = str(metadata.get("thread_user_id") or "").strip()
    if not user_id or metadata.get("background_job") or metadata.get("scheduled"):
        return 0
    try:
        user = await session.get(User, user_id)
        if not user or not user.is_active:
            return 0
        keys = await resolver_claves_entrega(session, user, task.agent_id, "progreso", task=task)
        return await _notificaciones(
            session,
            keys,
            user,
            "progreso",
            agent_id=task.agent_id,
            schedule_id=None,
            task_id=task.id,
            title="Sigo trabajando en tu solicitud",
            body=body,
        )
    except Exception:  # noqa: BLE001  # a notice problem must never touch the task itself
        logger.exception("Failed to enqueue progress notice for task %s", getattr(task, "id", None))
        return 0


async def enqueue_avance_de_trabajo(session: AsyncSession, task, body: str) -> int:
    metadata = task.metadata_json or {}
    user_id = str(metadata.get("thread_user_id") or "").strip()
    if not user_id or not metadata.get("background_job"):
        return 0
    user = await session.get(User, user_id)
    if not user or not user.is_active:
        return 0
    keys = await resolver_claves_entrega(session, user, task.agent_id, "progreso", task=task)
    titulo = str(metadata.get("background_title") or "tu análisis")[:200]
    return await _notificaciones(
        session,
        keys,
        user,
        "progreso",
        agent_id=task.agent_id,
        schedule_id=None,
        task_id=task.id,
        title=f"Avance: {titulo}"[:255],
        body=body,
    )


async def enqueue_handoff_notice(session: AsyncSession, task, title: str | None) -> int:
    metadata = task.metadata_json or {}
    user_id = str(metadata.get("thread_user_id") or "").strip()
    if not user_id or metadata.get("background_job") or metadata.get("scheduled"):
        return 0
    titulo = _clip(title or "tu solicitud", 200)
    body = (
        f"⏳ Tu solicitud era larga y no alcanzó a terminar en el chat; la sigo en segundo plano «{titulo}» "
        "y te aviso por aquí apenas esté lista. No hace falta reenviarla; si prefieres detenerla, escribe «cancela»."
    )
    try:
        user = await session.get(User, user_id)
        if not user or not user.is_active:
            return 0
        keys = await resolver_claves_entrega(session, user, task.agent_id, "traspaso", task=task)
        return await _notificaciones(
            session,
            keys,
            user,
            "traspaso",
            agent_id=task.agent_id,
            schedule_id=None,
            task_id=task.id,
            title="Sigo en segundo plano",
            body=body,
        )
    except Exception:  # noqa: BLE001  # a notice problem must never touch the task itself
        logger.exception("Failed to enqueue hand-off notice for task %s", getattr(task, "id", None))
        return 0


async def _agent_owner(session: AsyncSession, agent_id: str):
    result = await session.execute(
        select(PublicChatApiKey).where(
            PublicChatApiKey.agent_id == agent_id,
            PublicChatApiKey.is_active.is_(True),
            PublicChatApiKey.bound_user_id.isnot(None),
        )
    )
    candidatos = {str(key.bound_user_id) for key in result.scalars().all() if key.bound_user_id}
    humanos = []
    for user_id in sorted(candidatos):
        user = await session.get(User, user_id)
        if user is not None and not es_cuenta_de_servicio(getattr(user, "username", None)):
            humanos.append(user)
    return humanos[0] if len(humanos) == 1 else None


async def enqueue_delegation_notice(session: AsyncSession, task, *, success: bool, summary: str | None) -> int:
    metadata = task.metadata_json or {}
    if not metadata.get("delegated") or metadata.get("scheduled") or metadata.get("background_job"):
        return 0
    if not es_tarea_de_delegacion_del_servidor(task):
        return 0
    agent_id = task.agent_id
    try:
        owner = await _agent_owner(session, agent_id)
        if not owner or not owner.is_active:
            return 0
        keys = await resolver_claves_entrega(session, owner, agent_id, "delegacion", task=task)
        if not keys:
            return 0
        solicitante = "otro agente"
        src = getattr(task, "source_agent_id", None)
        if src:
            from hermeshq.models.agent import Agent

            ag = await session.get(Agent, src)
            if ag and ag.name:
                solicitante = ag.name
        titulo = str(metadata.get("title") or task.title or "una solicitud")[:200]
        if success:
            incompleto = motivo_incompleto(task)
            encabezado = f"Respuesta incompleta de tu agente ({incompleto})" if incompleto else "Respuesta de tu agente"
            body = f"📨 El agente «{solicitante}» le pidió a tu agente: «{titulo}».\n\n{encabezado}:\n" + (
                _clip(summary or "", MAX_BODY_CHARS) or "(sin contenido)"
            )
        else:
            detalle = _failure_body_detail(summary)
            body = (
                f"📨 El agente «{solicitante}» le pidió a tu agente: «{titulo}», pero tu agente no pudo "
                f"completarlo: " + _clip(detalle, MAX_FAILURE_CHARS)
            )
            if "determinar el usuario" in str(summary or ""):
                from hermeshq.services.delegation_approval import IDENTITY_TTL, new_identity_code

                codigo = new_identity_code()
                task.metadata_json = {
                    **metadata,
                    "identity_approval_code": codigo,
                    "delegation_owner_user_id": owner.id,
                    "identity_approval_expires": (datetime.now(UTC) + IDENTITY_TTL).isoformat(),
                }
                body += (
                    "\n\nTu agente necesita tu cuenta M365 para responder esto. Si autorizas que la use solo "
                    f"para esta solicitud, escribe: aprobar {codigo}. (O pídemelo tú directamente aquí y te lo "
                    "preparo.)"
                )
        return await _notificaciones(
            session,
            keys,
            owner,
            "delegacion",
            agent_id=agent_id,
            schedule_id=None,
            task_id=task.id,
            title=f"Solicitud de «{solicitante}»"[:255],
            body=body,
        )
    except Exception:  # noqa: BLE001  # a notice problem must never touch the task itself
        logger.exception("Failed to enqueue delegation notice for task %s", getattr(task, "id", None))
        return 0


def _tarea_es_del_cron(task, schedule) -> bool:
    if str(getattr(task, "agent_id", None) or "") != str(schedule.agent_id or ""):
        return False
    metadata = getattr(task, "metadata_json", None) or {}
    identidades = {
        str(valor)
        for valor in (getattr(task, "created_by_user_id", None), metadata.get("thread_user_id"))
        if valor not in (None, "")
    }
    return identidades == {str(schedule.owner_user_id)}


def es_tarea_de_delegacion_del_servidor(task) -> bool:
    metadata = getattr(task, "metadata_json", None) or {}
    return bool(
        getattr(task, "source_agent_id", None)
        and getattr(task, "created_by_user_id", None) in (None, "")
        and (metadata.get("delegated") or metadata.get("delegation_result"))
    )


async def enqueue_schedule_notification(session: AsyncSession, task, *, success: bool, summary: str | None) -> int:
    metadata = task.metadata_json or {}
    background = bool(metadata.get("background_job"))
    schedule_id = metadata.get("scheduled_task_id")
    if background:
        if not metadata.get("thread_user_id"):
            return 0
    elif (
        not metadata.get("scheduled")
        or not schedule_id
        or metadata.get("manual_trigger")
        and not metadata.get("solicitado_desde_chat")
    ):
        return 0
    try:
        if background:
            agent_id = task.agent_id
            owner_user_id = metadata["thread_user_id"]
            titulo = str(metadata.get("background_title") or "Trabajo en segundo plano")[:200]
            schedule_id = None
            notification_title = f"Trabajo en segundo plano: {titulo}"[:255]
        else:
            schedule = await session.get(ScheduledTask, schedule_id)
            if not schedule or not schedule.notify_public_chat or not schedule.owner_user_id:
                return 0
            if not _tarea_es_del_cron(task, schedule):
                logger.warning(
                    "Aviso de cron %s descartado: la tarea %s no pertenece al agente o al dueño del cron",
                    schedule.id,
                    getattr(task, "id", None),
                )
                return 0
            agent_id = schedule.agent_id
            owner_user_id = schedule.owner_user_id
            notification_title = (schedule.name + (" (ejecución manual)" if metadata.get("manual_trigger") else ""))[
                :255
            ]
        user = await session.get(User, owner_user_id)
        if not user or not user.is_active:
            return 0
        if background:
            keys = await resolver_claves_entrega(session, user, agent_id, "segundo_plano", task=task)
        else:
            keys = await resolver_claves_entrega(session, user, agent_id, "cron", task=task, schedule=schedule)
        if not keys:
            return 0
        incompleto = motivo_incompleto(task) if success else None
        if success:
            summary = _con_texto_si_solo_adjuntos(summary)
        if background:
            success, summary = _resumen_de_background(success, summary)
            if success and incompleto:
                body = (
                    f"⚠️ Tu trabajo en segundo plano «{titulo}» quedó incompleto: {incompleto}. "
                    "Esto es lo que alcancé:\n\n"
                    + (_clip(summary or "", MAX_BODY_CHARS) or "(sin contenido)")
                    + "\n\nSi quieres que lo continúe, pídemelo."
                )
            elif success and admite_que_no_pudo(summary):
                body = f"⚠️ Tu trabajo en segundo plano «{titulo}» terminó sin hacer lo pedido:\n\n" + _clip(
                    summary or "", MAX_BODY_CHARS
                )
            elif success and datos_no_obtenidos(task):
                body = (
                    f"⚠️ Tu trabajo en segundo plano «{titulo}» no pudo obtener sus datos (fallaron las consultas a "
                    "Microsoft 365):\n\n" + _clip(summary or "", MAX_BODY_CHARS)
                )
            elif success:
                body = f"✅ Terminó tu trabajo en segundo plano «{titulo}»:\n\n" + (
                    _clip(summary or "", MAX_BODY_CHARS) or "(terminó sin contenido)"
                )
            else:
                detalle = _failure_body_detail(summary)
                body = f"❌ No se pudo completar tu trabajo en segundo plano «{titulo}»: " + _clip(
                    detalle, MAX_FAILURE_CHARS
                )
        elif success and incompleto:
            body = f"⚠️ La tarea programada no alcanzó a terminar: {incompleto}. Resultado parcial:\n\n" + (
                _clip(summary or "", MAX_BODY_CHARS) or "(sin contenido)"
            )
        elif success and admite_que_no_pudo(summary):
            body = "⚠️ La tarea programada terminó sin hacer lo pedido:\n\n" + _clip(summary or "", MAX_BODY_CHARS)
        elif success and datos_no_obtenidos(task):
            body = (
                "⚠️ La tarea programada no pudo obtener sus datos (fallaron las consultas a Microsoft 365):\n\n"
                + _clip(summary or "", MAX_BODY_CHARS)
            )
        elif success:
            body = _clip(summary or "", MAX_BODY_CHARS) or "(la tarea terminó sin contenido)"
        else:
            detalle = _failure_body_detail(summary)
            body = f"La tarea programada falló: {_clip(detalle, MAX_FAILURE_CHARS)}"
        body = tablas_para_teams(quitar_lineas_media(body))
        return await _notificaciones(
            session,
            keys,
            user,
            "segundo_plano" if background else "cron",
            agent_id=agent_id,
            schedule_id=schedule_id,
            task_id=task.id,
            title=notification_title,
            body=body,
        )
    except Exception:  # noqa: BLE001  # a notification problem must never fail the task itself
        logger.exception("Failed to enqueue schedule notification for task %s", getattr(task, "id", None))
        return 0


async def purge_old_notifications(session: AsyncSession, api_key_id: str) -> None:
    cutoff = datetime.now(UTC) - NOTIFICATION_RETENTION
    await session.execute(
        delete(PublicChatNotification).where(
            PublicChatNotification.api_key_id == api_key_id,
            PublicChatNotification.created_at < cutoff,
        )
    )
