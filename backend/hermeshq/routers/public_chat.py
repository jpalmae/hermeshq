import asyncio
import ipaddress
import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.public_chat import PublicChatApiKey, PublicChatNotification, PublicChatSession
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.routers.attachments import _resolve_media_type
from hermeshq.schemas.public_chat import (
    ApiKeyCreatedResponse,
    ApiKeyRead,
    CloseSessionRequest,
    CreateApiKeyRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    NotificationRead,
    SendMessageRequest,
    SessionStatusResponse,
    TranscriptRead,
    UpdateApiKeyRequest,
)
from hermeshq.services.public_chat_service import BindingError
from hermeshq.services.schedule_notifications import (
    NOTIFICATION_DELIVERY_WINDOW,
    friendly_error_text,
    purge_old_notifications,
)
from hermeshq.services.texto_chat import PREFIJO_CLAVE_TEAMS, respuesta_para_canal

logger = logging.getLogger(__name__)

TEXTO_RESPUESTA_TARDIA = (
    "⏳ Tu solicitud sigue en curso y tarda más de lo habitual. Te envío la respuesta por aquí apenas "
    "termine; no hace falta reenviarla."
)


async def _entregar_por_notificacion_si_hace_falta(request: Request, task_id: str) -> None:
    from hermeshq.services.schedule_notifications import enqueue_respuesta_tardia

    try:
        async with request.app.state.supervisor.session_factory() as db:
            task = await db.get(Task, task_id)
            if task is None or (task.metadata_json or {}).get("respuesta_tardia"):
                return
            metadata = dict(task.metadata_json or {})
            metadata["respuesta_tardia"] = True
            task.metadata_json = metadata
            if task.status == "completed":
                await enqueue_respuesta_tardia(db, task, success=True, summary=task.response)
            elif task.status == "failed":
                await enqueue_respuesta_tardia(db, task, success=False, summary=task.error_message)
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo marcar la respuesta tardía de la tarea %s", task_id)


public_router = APIRouter(prefix="/api/public/chat", tags=["public-chat"])
management_router = APIRouter(prefix="/settings/public-chat-keys", tags=["public-chat-management"])


def _friendly_task_error(event: dict) -> str:
    error_type = str(event.get("error_type") or "")
    raw_error = str(event.get("error") or "")
    if error_type == "QueueTimeout":
        return (
            "Tu solicitud anterior sigue en curso, así que este mensaje no se procesó. "
            "Espera a que te responda y vuelve a enviarlo, o escribe «cancela» para detener lo anterior."
        )
    if event.get("auto_background"):
        return (
            "Tu solicitud es larga y no alcanzó a terminar aquí. La sigo en segundo plano y te aviso por este chat "
            "apenas esté lista; no hace falta reenviarla. Si prefieres detenerla, escribe «cancela»."
        )
    return friendly_error_text(raw_error)


_PUBLIC_ERROR_MAP = {
    "Invalid or inactive API key": ("Invalid or inactive API key", 401),
    "Origin not allowed": ("Access denied", 403),
    "Rate limit exceeded": ("Too many requests, please wait", 429),
    "Monthly request quota exceeded": ("Monthly request limit reached", 429),
    "Monthly token quota exceeded": ("Monthly token limit reached", 429),
    "Too many active sessions": ("Too many active sessions", 429),
    "Session not found or inactive": ("Session unavailable", 400),
    "Session expired": ("Session expired", 400),
    "Agent is not available": ("Service temporarily unavailable", 503),
    "Agent not found": ("Service temporarily unavailable", 503),
}


def _get_service(request: Request):
    return request.app.state.public_chat_service


def _proxy_de_confianza(ip: str) -> bool:
    try:
        direccion = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for red in get_settings().public_chat_trusted_proxies:
        try:
            if direccion in ipaddress.ip_network(red, strict=False):
                return True
        except ValueError:
            logger.warning("Proxy de confianza inválido en public_chat_trusted_proxies: %s", red)
    return False


def _get_client_ip(request: Request) -> str:
    client = getattr(request, "client", None)
    ip_socket = (getattr(client, "host", "") or "") if client else ""
    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip and _proxy_de_confianza(ip_socket):
        return real_ip
    return ip_socket


VENTANA_CLAVE_COMPARTIDA = timedelta(minutes=5)


def _error_con_codigo(status: int, code: str, mensaje: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "codigo": code, "error": mensaje})


async def validar_canal(request: Request, db: AsyncSession, api_key):
    from hermeshq.services.canales_bot import canal_de_clave

    slug = (request.headers.get("x-bot-channel") or "").strip()
    if not slug and not getattr(api_key, "bot_channel_id", None):
        return None
    canal = await canal_de_clave(db, api_key)
    if canal is None:
        if getattr(api_key, "bot_channel_id", None):
            raise _error_con_codigo(403, "canal_incorrecto", "El canal de la clave no existe")
        return None
    if slug:
        if slug != canal.slug:
            logger.warning("Clave %s usada desde el canal %s; su canal es %s", api_key.id, slug, canal.slug)
            raise _error_con_codigo(403, "canal_incorrecto", "La clave no pertenece a este canal de bot")
    elif canal.require_channel_header:
        raise _error_con_codigo(403, "canal_incorrecto", "Falta la cabecera X-Bot-Channel")
    else:
        logger.info("Clave %s usada sin X-Bot-Channel (canal %s)", api_key.id, canal.slug)
    return canal


def _registrar_instancia(request: Request, api_key, canal) -> None:
    instancia = (request.headers.get("x-bridge-instance") or "").strip()[:64]
    if not instancia:
        return
    anterior = getattr(api_key, "last_poll_instance", None)
    ultimo = getattr(api_key, "last_notification_poll_at", None)
    if anterior and anterior != instancia and ultimo and datetime.now(UTC) - ultimo < VENTANA_CLAVE_COMPARTIDA:
        logger.error("clave_compartida: la clave %s la sondean dos bridges (%s y %s)", api_key.id, anterior, instancia)
        if canal is not None and canal.require_channel_header:
            raise _error_con_codigo(409, "clave_compartida", "Esta clave ya la usa otro bridge")
    api_key.last_poll_instance = instancia


def _public_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    safe_msg, status = _PUBLIC_ERROR_MAP.get(msg, ("Something went wrong", 400))
    if msg not in _PUBLIC_ERROR_MAP:
        logger.warning("Unmapped public chat error: %s", msg)
    return HTTPException(status_code=status, detail=safe_msg)


# ── Public endpoints (API key + session token auth) ──


@public_router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(
    payload: CreateSessionRequest,
    request: Request,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    origin = request.headers.get("origin")
    client_ip = _get_client_ip(request)
    try:
        api_key = await service.validate_api_key(x_api_key, db, origin=origin, client_ip=client_ip)
    except ValueError as e:
        raise _public_error(e)
    await validar_canal(request, db, api_key)
    try:
        result = await service.create_session(
            api_key, db, client_ip=client_ip, sender_id=payload.sender_id, aad_object_id=payload.aad_object_id
        )
    except ValueError as e:
        if str(e) == "Sender is not the bound user":
            raise _error_con_codigo(403, "remitente_no_vinculado", "El remitente no es el usuario de esta clave")
        raise _public_error(e)
    return result


async def _es_clave_teams(db: AsyncSession, session_id: str) -> bool:
    fila = await db.execute(
        select(PublicChatApiKey.label)
        .join(PublicChatSession, PublicChatSession.api_key_id == PublicChatApiKey.id)
        .where(PublicChatSession.id == session_id)
    )
    etiqueta = fila.scalar_one_or_none()
    return isinstance(etiqueta, str) and etiqueta.startswith(PREFIJO_CLAVE_TEAMS)


@public_router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    payload: SendMessageRequest,
    request: Request,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    client_ip = _get_client_ip(request)
    try:
        cancel_reply = await service.handle_cancel_request(
            session_id, x_session_token, payload.content, db, client_ip=client_ip
        )
    except ValueError as e:
        raise _public_error(e)
    if cancel_reply is not None:
        await db.close()

        async def cancel_stream():
            yield f"data: {json.dumps({'type': 'done', 'content': cancel_reply, 'attachments': []})}\n\n"

        return StreamingResponse(cancel_stream(), media_type="text/event-stream")
    try:
        task_id, agent_id = await service.send_message(
            session_id, x_session_token, payload.content, db, client_ip=client_ip
        )
    except ValueError as e:
        raise _public_error(e)
    es_teams = task_id is not None and await _es_clave_teams(db, session_id)
    await db.close()

    if task_id is None:
        inflight_reply = agent_id

        async def inflight_stream():
            yield f"data: {json.dumps({'type': 'done', 'content': inflight_reply, 'attachments': []})}\n\n"

        return StreamingResponse(inflight_stream(), media_type="text/event-stream")

    async def sse_stream():
        broker = request.app.state.event_broker
        queue: asyncio.Queue = asyncio.Queue()
        final_response = []

        async def event_handler(event: dict) -> None:
            if event.get("task_id") != task_id:
                return
            event_type = event.get("type", "")
            if event_type == "task.progress":
                await queue.put(("stream", event.get("message", ""), None))
            elif event_type == "task.completed":
                attachments = (event.get("metadata") or {}).get("response_attachments") or []
                await queue.put(("done", event.get("response", ""), attachments))
            elif event_type == "task.cancelled":
                await queue.put(("cancelled", "⏹️ Esta solicitud fue cancelada.", None))
            elif event_type == "task.failed":
                raw_error = event.get("error", "Unknown error")
                logger.error("Public chat task %s failed: %s", task_id, raw_error)
                await queue.put(("error", _friendly_task_error(event), None))

        broker.subscribe(event_handler)
        try:
            keepalive_count = 0
            # Force-closes the stream after this many consecutive 15s polls
            # with no event at all — i.e. the agent has gone fully silent
            # (no task.progress) for this long. Previously 8 (120s), which
            # is well under the task's own timeout (TASK_TIMEOUT_SECONDS,
            # default 3600s) and cut off real, slow-but-still-running
            # responses (a tool call with no intermediate progress emission
            # easily exceeds 2 minutes). Raised to 40 (600s) — still bounded,
            # but gives a genuinely slow turn room to finish. Fixed 2026-09-15.
            _MAX_KEEPALIVES_BEFORE_TIMEOUT = 40
            while True:
                try:
                    event_type, data, attachments = await asyncio.wait_for(queue.get(), timeout=15)
                    keepalive_count = 0
                except TimeoutError:
                    keepalive_count += 1
                    if keepalive_count >= _MAX_KEEPALIVES_BEFORE_TIMEOUT:
                        await _entregar_por_notificacion_si_hace_falta(request, task_id)
                        await service.save_assistant_message(session_id, TEXTO_RESPUESTA_TARDIA)
                        yield f"data: {json.dumps({'type': 'done', 'content': TEXTO_RESPUESTA_TARDIA, 'attachments': []})}\n\n"
                        break
                    yield ": keepalive\n\n"
                    continue

                if event_type == "stream":
                    final_response.append(data)
                    yield f"data: {json.dumps({'type': 'stream', 'content': data})}\n\n"
                elif event_type == "done":
                    # Prefer `data` (task.completed's own response field) over
                    # the raw joined stream chunks: hermes_runtime.py strips
                    # MEDIA: lines and repairs sandbox:/file: links on the
                    # final response text, but streamed task.progress chunks
                    # are the *pre*-cleanup text as it was generated token by
                    # token — reconstructing from them re-introduces exactly
                    # what the cleanup was supposed to remove. Only fall back
                    # to the joined stream if the completion event somehow
                    # carried no response at all. Fixed 2026-09-15.
                    full_response = data if data else "".join(final_response)
                    texto_canal = respuesta_para_canal(full_response, es_teams)
                    safe_attachments = [
                        {
                            "file_id": att.get("file_id"),
                            "filename": att.get("filename"),
                            "media_type": att.get("media_type"),
                            "size": att.get("size"),
                        }
                        for att in (attachments or [])
                        if att.get("file_id")
                    ]
                    yield (
                        "data: "
                        f"{json.dumps({'type': 'done', 'content': texto_canal, 'attachments': safe_attachments})}"
                        "\n\n"
                    )
                    await service.save_assistant_message(session_id, full_response)
                    break
                elif event_type == "cancelled":
                    yield f"data: {json.dumps({'type': 'done', 'content': data, 'attachments': []})}\n\n"
                    await service.save_assistant_message(session_id, data)
                    break
                elif event_type == "error":
                    yield f"data: {json.dumps({'type': 'error', 'content': data})}\n\n"
                    await service.renovar_actividad(session_id)
                    break
        finally:
            broker.unsubscribe(event_handler)

    return StreamingResponse(sse_stream(), media_type="text/event-stream")


@public_router.get("/notifications", response_model=list[NotificationRead])
async def list_notifications(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50),
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    try:
        api_key = await service.validate_api_key(
            x_api_key, db, origin=request.headers.get("origin"), client_ip=_get_client_ip(request)
        )
    except ValueError as e:
        raise _public_error(e)
    if not service.notification_limiter.check(api_key.id):
        raise HTTPException(status_code=429, detail="Too many requests, please wait")
    canal = await validar_canal(request, db, api_key)
    _registrar_instancia(request, api_key, canal)
    api_key.last_notification_poll_at = datetime.now(UTC)
    await purge_old_notifications(db, api_key.id)
    filtros = [
        PublicChatNotification.api_key_id == api_key.id,
        PublicChatNotification.status == "pending",
        PublicChatNotification.created_at > datetime.now(UTC) - NOTIFICATION_DELIVERY_WINDOW,
    ]
    usuario_fijo = getattr(api_key, "bound_user_id", None)
    if usuario_fijo:
        filtros.append(PublicChatNotification.recipient_user_id == usuario_fijo)
    result = await db.execute(
        select(PublicChatNotification).where(*filtros).order_by(PublicChatNotification.created_at.asc()).limit(limit)
    )
    notificaciones = list(result.scalars().all())
    tareas = await _tareas_de(db, [n.task_id for n in notificaciones if n.task_id])
    items = []
    for notificacion in notificaciones:
        item = NotificationRead.model_validate(notificacion, from_attributes=True)
        item.attachments = _adjuntos_publicos(tareas.get(notificacion.task_id))
        items.append(item)
    await db.commit()
    return items


@public_router.get("/notifications/{notification_id}/attachments/{file_id}")
async def download_notification_attachment(
    notification_id: str,
    file_id: str,
    request: Request,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    try:
        api_key = await service.validate_api_key(
            x_api_key, db, origin=request.headers.get("origin"), client_ip=_get_client_ip(request)
        )
    except ValueError as e:
        raise _public_error(e)
    if not service.notification_limiter.check(api_key.id):
        raise HTTPException(status_code=429, detail="Too many requests, please wait")
    await validar_canal(request, db, api_key)
    notificacion = await db.get(PublicChatNotification, notification_id)
    if notificacion is None or notificacion.api_key_id != api_key.id or not notificacion.task_id:
        raise HTTPException(status_code=404, detail="Attachment not found")
    usuario_fijo = getattr(api_key, "bound_user_id", None)
    if usuario_fijo and str(notificacion.recipient_user_id or "") != str(usuario_fijo):
        raise HTTPException(status_code=404, detail="Attachment not found")
    tareas = await _tareas_de(db, [notificacion.task_id])
    adjunto = next(
        (a for a in _adjuntos_publicos(tareas.get(notificacion.task_id)) if a.get("file_id") == file_id), None
    )
    if adjunto is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    workspace = request.app.state.workspace_manager.build_workspace_path(notificacion.agent_id)
    matches = list((workspace / "uploads").glob(f"{file_id}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail="Attachment not found")
    filename = adjunto.get("filename") or matches[0].name
    media_type = adjunto.get("media_type") or _resolve_media_type(filename)
    return FileResponse(matches[0], media_type=media_type, filename=filename)


async def _tareas_de(db: AsyncSession, task_ids: list[str]) -> dict:
    if not task_ids:
        return {}
    result = await db.execute(select(Task).where(Task.id.in_(task_ids)))
    return {tarea.id: tarea for tarea in result.scalars().all()}


def _adjuntos_publicos(tarea) -> list[dict]:
    if tarea is None:
        return []
    return [
        {
            "file_id": att.get("file_id"),
            "filename": att.get("filename"),
            "media_type": att.get("media_type"),
            "size": att.get("size"),
        }
        for att in (tarea.metadata_json or {}).get("response_attachments") or []
        if att.get("file_id")
    ]


async def _renovar_sesion_de_la_entrega(service, db: AsyncSession, notification, api_key_id: str) -> None:
    if not getattr(notification, "task_id", None):
        return
    try:
        tarea = await db.get(Task, notification.task_id)
        metadata = getattr(tarea, "metadata_json", None)
        session_id = str((metadata if isinstance(metadata, dict) else {}).get("public_session_id") or "")
        if not session_id:
            return
        sesion = await db.get(PublicChatSession, session_id)
        if sesion is not None and getattr(sesion, "api_key_id", None) == api_key_id:
            await service.renovar_actividad(session_id, db)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo renovar la sesión de la notificación %s", notification.id)


@public_router.post("/notifications/{notification_id}/ack")
async def acknowledge_notification(
    notification_id: str,
    request: Request,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    try:
        api_key = await service.validate_api_key(
            x_api_key, db, origin=request.headers.get("origin"), client_ip=_get_client_ip(request)
        )
    except ValueError as e:
        raise _public_error(e)
    if not service.notification_limiter.check(api_key.id):
        raise HTTPException(status_code=429, detail="Too many requests, please wait")
    await validar_canal(request, db, api_key)
    notification = await db.get(PublicChatNotification, notification_id)
    if not notification or notification.api_key_id != api_key.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notification.status != "delivered":
        notification.status = "delivered"
        notification.delivered_at = datetime.now(UTC)
        await _renovar_sesion_de_la_entrega(service, db, notification, api_key.id)
        await db.commit()
    return {"acknowledged": True, "id": notification.id}


@public_router.post("/sessions/{session_id}/close")
async def close_session(
    session_id: str,
    request: Request,
    payload: CloseSessionRequest | None = None,
    x_session_token: str | None = Header(None, alias="X-Session-Token"),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    token = x_session_token
    if not token and payload:
        token = payload.session_token
    if not token:
        raise HTTPException(status_code=400, detail="Session token required")
    await service.close_session(session_id, token, db)
    return {"status": "closed"}


@public_router.get("/sessions/{session_id}/status", response_model=SessionStatusResponse)
async def session_status(
    session_id: str,
    request: Request,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    try:
        result = await service.get_session_status(session_id, x_session_token, db)
    except ValueError as e:
        raise _public_error(e)
    return result


@public_router.get("/sessions/{session_id}/attachments/{file_id}")
async def download_attachment(
    session_id: str,
    file_id: str,
    request: Request,
    x_session_token: str | None = Header(None, alias="X-Session-Token"),
    token: str | None = Query(
        None, description="Session token — fallback for plain <a href> links, which can't set headers"
    ),
    db: AsyncSession = Depends(get_db_session),
):
    """Serve a file the agent generated during this public chat session.

    Auth accepts the session token via header (fetch/XHR clients) or query
    param (the widget's own download links, since a plain anchor tag can't
    send custom headers). Ownership is checked by requiring the file_id to
    appear in a task's response_attachments *and* that task to belong to
    this exact session_id — a stolen/guessed file_id from another session
    won't resolve here.
    """
    service = _get_service(request)
    session_token = x_session_token or token
    if not session_token:
        raise HTTPException(status_code=400, detail="Session token required")
    try:
        session = await service.validate_session(session_id, session_token, db)
    except ValueError as e:
        raise _public_error(e)

    result = await db.execute(
        select(Task)
        .where(Task.metadata_json["public_session_id"].as_string() == session_id)
        .order_by(Task.queued_at.desc())
    )
    attachment = None
    for task in result.scalars():
        for att in (task.metadata_json or {}).get("response_attachments") or []:
            if att.get("file_id") == file_id:
                attachment = att
                break
        if attachment:
            break
    if not attachment:
        raise HTTPException(status_code=404, detail="Attachment not found")

    workspace = request.app.state.workspace_manager.build_workspace_path(session.agent_id)
    uploads = workspace / "uploads"
    matches = list(uploads.glob(f"{file_id}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail="Attachment not found")

    file_path = matches[0]
    filename = attachment.get("filename") or file_path.name
    media_type = attachment.get("media_type") or _resolve_media_type(filename)
    return FileResponse(file_path, media_type=media_type, filename=filename)


# ── Management endpoints (admin-only JWT auth) ──


@management_router.post("", response_model=ApiKeyCreatedResponse)
async def create_api_key(
    payload: CreateApiKeyRequest,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    try:
        result = await service.create_api_key(
            label=payload.label,
            agent_id=payload.agent_id,
            allowed_domains=payload.allowed_domains,
            requests_per_month=payload.requests_per_month,
            tokens_per_month=payload.tokens_per_month,
            db=db,
            widget_title=payload.widget_title,
            widget_theme=payload.widget_theme,
            widget_accent=payload.widget_accent,
            widget_position=payload.widget_position,
            bound_user_id=payload.bound_user_id,
            trust_sender_id=payload.trust_sender_id,
            bot_channel_id=payload.bot_channel_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@management_router.get("", response_model=list[ApiKeyRead])
async def list_api_keys(
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    return await service.list_api_keys(db)


@management_router.patch("/{key_id}", response_model=ApiKeyRead)
async def update_api_key(
    key_id: str,
    payload: UpdateApiKeyRequest,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    try:
        return await service.update_api_key(key_id, payload, db)
    except BindingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@management_router.delete("/{key_id}")
async def deactivate_api_key(
    key_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    try:
        await service.delete_api_key(key_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "deactivated"}


@management_router.delete("/{key_id}/permanent")
async def permanently_delete_api_key(
    key_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    try:
        await service.permanently_delete_api_key(key_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "deleted"}


@management_router.get("/{key_id}/transcripts", response_model=list[TranscriptRead])
async def list_transcripts(
    key_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    service = _get_service(request)
    return await service.list_transcripts(key_id, db)
