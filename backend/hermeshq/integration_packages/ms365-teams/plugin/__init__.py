from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOOLSET = "hermeshq_ms365_teams"

# Graph's simple PUT .../content upload only supports files up to 4 MiB;
# larger files need a chunked upload session, which we don't implement here.
_MAX_SIMPLE_UPLOAD_BYTES = 4 * 1024 * 1024
_UPLOAD_FOLDER = "HermesHQ Uploads"


def _resolve_session_user_id() -> str | None:
    """Resolve the HermesHQ user for the in-flight live gateway turn.

    Hermes Agent tracks the platform/sender of the current turn per-message
    via contextvars (safe under concurrent messages in the same gateway
    process), exposed through these env vars. Unused for punctual tasks,
    which carry their user in HERMESHQ_TASK_PAYLOAD instead.
    """
    platform = os.environ.get("HERMES_SESSION_PLATFORM", "").strip().lower()
    sender_id = os.environ.get("HERMES_SESSION_USER_ID", "").strip()
    if not platform or not sender_id:
        return None
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    agent_id = os.environ.get("HERMESHQ_AGENT_ID", "")
    agent_token = os.environ.get("HERMESHQ_AGENT_TOKEN", "")
    if not base_url or not agent_id or not agent_token:
        return None
    url = (
        f"{base_url}/control/resolve-channel-user"
        f"?platform={urllib.parse.quote(platform)}&sender_id={urllib.parse.quote(sender_id)}"
    )
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "X-HermesHQ-Agent-ID": agent_id,
            "X-HermesHQ-Agent-Token": agent_token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return str(data.get("hermeshq_user_id") or "").strip() or None
    except urllib.error.HTTPError:
        return None
    except Exception:  # noqa: BLE001  # HTTP request catch-all
        return None


def _task_user_id() -> str | None:
    raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    if raw:
        try:
            payload = json.loads(raw)
            meta = payload.get("metadata") or {}
            uid = str(meta.get("thread_user_id") or meta.get("created_by_user_id") or "").strip()
            if uid:
                return uid
        except (json.JSONDecodeError, ValueError):
            pass
    resolved = _resolve_session_user_id()
    if resolved:
        return resolved
    return os.environ.get("HERMESHQ_RESOLVED_USER_ID") or None


def _get_m365_token(user_id: str) -> tuple[str | None, str]:
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    agent_id = os.environ.get("HERMESHQ_AGENT_ID", "")
    agent_token = os.environ.get("HERMESHQ_AGENT_TOKEN", "")
    if not base_url or not agent_id or not agent_token:
        return None, "HermesHQ internal control no configurado"
    url = f"{base_url}/control/m365/agent-token?user_id={user_id}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"X-HermesHQ-Agent-ID": agent_id, "X-HermesHQ-Agent-Token": agent_token},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("access_token"), ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail", body)
        except (json.JSONDecodeError, ValueError):
            detail = body
        return None, str(detail)
    except Exception as exc:  # noqa: BLE001  # HTTP request catch-all
        return None, str(exc)


def _encode_url(url: str) -> str:
    return urllib.parse.quote(url, safe="%:/?#[]@!$&'()*+,;=~")


def _graph(method: str, path: str, access_token: str, payload: dict | None = None) -> dict:
    url = _encode_url(path if path.startswith("https://") else f"{GRAPH_BASE}{path}")
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method.upper(),
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        try:
            parsed = json.loads(body)
            err = parsed.get("error", {})
            code = err.get("code", "")
            msg = err.get("message", body)
        except (json.JSONDecodeError, ValueError):
            code, msg = "", body
        _FRIENDLY = {
            "Authorization_RequestDenied": "Sin permiso para acceder a este recurso (403).",
            "accessDenied": "Sin permiso para acceder a este recurso (403).",
            "itemNotFound": "Chat o elemento no encontrado (404).",
            "ActivityLimitReached": f"Límite de peticiones alcanzado. Reintenta en {retry_after or '60'} segundos.",
        }
        friendly = _FRIENDLY.get(code) or (f"Error {exc.code}: {msg}" if msg else f"Error HTTP {exc.code}")
        result: dict = {"error": friendly, "_graph_code": code}
        if retry_after:
            result["retry_after"] = retry_after
        return result


def _upload_file_to_onedrive(access_token: str, file_path: str) -> tuple[dict | None, str]:
    """Upload a local file to the user's OneDrive and return the created driveItem.

    Used to turn a MEDIA:-style local path into something that can actually be
    attached to a Teams chat message (Graph has no "attach raw bytes to a chat
    message" endpoint — a chat attachment is a *reference* to a driveItem).
    Returns (driveItem, "") on success, or (None, friendly_error) on failure.
    """
    path = Path(file_path)
    if not path.is_file():
        return None, f"Archivo no encontrado: {file_path}"

    size = path.stat().st_size
    if size > _MAX_SIMPLE_UPLOAD_BYTES:
        return None, (
            f"El archivo pesa {size / (1024 * 1024):.1f} MB; el límite para adjuntar "
            f"en Teams es {_MAX_SIMPLE_UPLOAD_BYTES / (1024 * 1024):.0f} MB."
        )

    # A fresh uuid prefix avoids clashing with a previous upload of the same
    # filename sitting in the folder (OneDrive would otherwise auto-rename it,
    # e.g. "hola (1).pdf", which is confusing in the chat).
    dest_name = f"{uuid.uuid4().hex[:8]}_{path.name}"
    encoded_folder = urllib.parse.quote(_UPLOAD_FOLDER)
    encoded_name = urllib.parse.quote(dest_name)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    url = f"{GRAPH_BASE}/me/drive/root:/{encoded_folder}/{encoded_name}:/content"
    req = urllib.request.Request(
        url,
        data=path.read_bytes(),
        method="PUT",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            drive_item = json.loads(resp.read().decode("utf-8"))
            return drive_item, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(body).get("error", {}).get("message", body)
        except (json.JSONDecodeError, ValueError):
            msg = body
        return None, f"No se pudo subir el archivo a OneDrive: {msg}"
    except Exception as exc:  # noqa: BLE001  # HTTP request catch-all
        return None, f"No se pudo subir el archivo a OneDrive: {exc}"


def _auth_error(detail: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": f"No se pudo obtener token M365: {detail}. Verifica que el usuario haya conectado su cuenta Microsoft 365 en Mi cuenta.",
        }
    )


# ── Tool handlers ─────────────────────────────────────────────────────────────


def _list_teams_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    result = _graph("GET", "/me/joinedTeams", token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    teams = result.get("value", [])
    simplified = [{"id": t.get("id"), "name": t.get("displayName"), "description": t.get("description")} for t in teams]
    return json.dumps({"success": True, "count": len(simplified), "teams": simplified}, ensure_ascii=False)


def _list_chats_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    count = min(int(args.get("count") or 20), 50)
    result = _graph("GET", f"/me/chats?$top={count}&$expand=members", token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    chats = result.get("value", [])
    simplified = []
    for c in chats:
        members = [m.get("displayName") for m in (c.get("members") or []) if m.get("displayName")]
        simplified.append(
            {
                "id": c.get("id"),
                "topic": c.get("topic") or ", ".join(members[:3]),
                "type": c.get("chatType"),
                "members": members,
            }
        )
    return json.dumps({"success": True, "count": len(simplified), "chats": simplified}, ensure_ascii=False)


def _get_chat_messages_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    chat_id = str(args.get("chat_id") or "").strip()
    if not chat_id:
        return json.dumps({"success": False, "error": "Se requiere chat_id."})
    count = min(int(args.get("count") or 20), 50)
    result = _graph("GET", f"/me/chats/{chat_id}/messages?$top={count}", token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    messages = result.get("value", [])
    simplified = [
        {
            "id": m.get("id"),
            "from": (m.get("from") or {}).get("user", {}).get("displayName"),
            "body": (m.get("body") or {}).get("content", ""),
            "created": m.get("createdDateTime"),
        }
        for m in messages
    ]
    return json.dumps({"success": True, "count": len(simplified), "messages": simplified}, ensure_ascii=False)


def _send_chat_message_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    chat_id = str(args.get("chat_id") or "").strip()
    message = str(args.get("message") or "").strip()
    file_path = str(args.get("file_path") or "").strip()
    if not chat_id or (not message and not file_path):
        return json.dumps({"success": False, "error": "Se requiere chat_id y al menos message o file_path."})

    attachments = []
    if file_path:
        drive_item, upload_err = _upload_file_to_onedrive(token, file_path)
        if not drive_item:
            return json.dumps({"success": False, "error": upload_err})
        attachment_id = drive_item.get("id", "")
        attachments.append(
            {
                "id": attachment_id,
                "contentType": "reference",
                "contentUrl": drive_item.get("webUrl", ""),
                "name": drive_item.get("name", Path(file_path).name),
            }
        )
        # Teams requires an <attachment> tag inside an HTML body to actually
        # render the reference as a file card; a plain-text body with an
        # `attachments` array alone is silently ignored by the client.
        body_html = (
            f'{message}<attachment id="{attachment_id}"></attachment>'
            if message
            else f'<attachment id="{attachment_id}"></attachment>'
        )
        payload = {"body": {"contentType": "html", "content": body_html}, "attachments": attachments}
    else:
        payload = {"body": {"contentType": "text", "content": message}}

    result = _graph("POST", f"/me/chats/{chat_id}/messages", token, payload)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    return json.dumps({"success": True, "message": "Mensaje enviado correctamente.", "id": result.get("id")})


def _find_user(token: str, email: str) -> tuple[dict | None, str]:
    limpio = email.replace("'", "''")
    consulta = urllib.parse.quote(f"mail eq '{limpio}' or userPrincipalName eq '{limpio}'", safe="")
    resultado = _graph("GET", f"/users?$filter={consulta}&$select=id,displayName,mail,userPrincipalName", token)
    if "error" in resultado:
        return None, str(resultado["error"])
    encontrados = resultado.get("value", [])
    if not encontrados:
        return None, f"No encontré a '{email}' en el directorio de la empresa."
    return encontrados[0], ""


def _find_direct_chat(token: str, person_id: str) -> str | None:
    ruta = "/me/chats?$filter=chatType eq 'oneOnOne'&$expand=members&$top=50"
    for _ in range(4):
        resultado = _graph("GET", ruta, token)
        if "error" in resultado:
            return None
        for chat in resultado.get("value", []):
            if any(m.get("userId") == person_id for m in chat.get("members") or []):
                return chat.get("id")
        ruta = resultado.get("@odata.nextLink") or ""
        if not ruta:
            return None
    return None


_APPROVAL_TTL_SECONDS = 1800


def _current_task_id() -> str:
    raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    try:
        return str(json.loads(raw).get("task_id") or "") if raw else ""
    except (ValueError, AttributeError):
        return ""


def _previews_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".teams_previews.json"


def _load_previews() -> dict:
    try:
        data = json.loads(_previews_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    limit = time.time() - _APPROVAL_TTL_SECONDS
    return {k: v for k, v in data.items() if isinstance(v, dict) and float(v.get("ts") or 0) >= limit}


def _save_previews(previews: dict) -> None:
    try:
        path = _previews_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(previews), encoding="utf-8")
    except OSError:
        pass


def _message_fingerprint(user_id: str, person_id: str, message: str, file_path: str) -> str:
    return hashlib.sha256(f"{user_id}|{person_id}|{message}|{file_path}".encode()).hexdigest()


def _remember_preview(fingerprint: str) -> None:
    previews = _load_previews()
    previews[fingerprint] = {"task": _current_task_id(), "ts": time.time()}
    _save_previews(previews)


def _approval_is_real(fingerprint: str) -> bool:
    """La aprobación vale solo si la vista previa idéntica se mostró en una tarea ANTERIOR (el usuario respondió después)."""
    entry = _load_previews().get(fingerprint)
    current = _current_task_id()
    return bool(entry and current and entry.get("task") and entry["task"] != current)


def _send_to_person_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    email = str(args.get("email") or "").strip()
    message = str(args.get("message") or "").strip()
    file_path = str(args.get("file_path") or "").strip()
    if "@" not in email or (not message and not file_path):
        return json.dumps(
            {
                "success": False,
                "error": "Se requiere el correo exacto de la persona (email) y message o file_path. "
                "Si solo tienes el nombre, búscalo antes con ms365_find_person y confírmalo con el usuario.",
            },
            ensure_ascii=False,
        )
    persona, error = _find_user(token, email)
    if persona is None:
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    para = {"nombre": persona.get("displayName"), "correo": persona.get("mail") or email}
    fingerprint = _message_fingerprint(user_id, str(persona.get("id")), message, file_path)
    if not args.get("aprobado_por_el_usuario") or not _approval_is_real(fingerprint):
        _remember_preview(fingerprint)
        resultado = {
            "success": False,
            "requiere_aprobacion": True,
            "vista_previa": {"para": para, "mensaje": message, "archivo": file_path or None},
            "siguiente": "Muéstrale al usuario esta vista previa (a quién y qué texto) y pregunta si lo envías; "
            "termina tu respuesta ahí y espera su siguiente mensaje. Cuando diga que sí, vuelve a llamar con "
            "aprobado_por_el_usuario=true y EXACTAMENTE los mismos datos.",
        }
        if args.get("aprobado_por_el_usuario"):
            resultado["error"] = (
                "No se envió: esta vista previa aún no se le mostró al usuario en un mensaje anterior. No puedes "
                "aprobar por él en la misma respuesta."
            )
        return json.dumps(resultado, ensure_ascii=False)
    chat_id = _find_direct_chat(token, str(persona.get("id")))
    creado = False
    if not chat_id:
        yo = _graph("GET", "/me?$select=id", token)
        if "error" in yo:
            return json.dumps({"success": False, "error": yo["error"]}, ensure_ascii=False)
        base = f"{GRAPH_BASE}/users"
        nuevo = _graph(
            "POST",
            "/chats",
            token,
            {
                "chatType": "oneOnOne",
                "members": [
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": f"{base}('{yo['id']}')",
                    },
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": f"{base}('{persona['id']}')",
                    },
                ],
            },
        )
        if "error" in nuevo:
            detalle = str(nuevo["error"])
            if "403" in detalle:
                detalle += (
                    " Falta el permiso Chat.Create: el usuario debe reconectar su cuenta Microsoft 365 en Mi cuenta."
                )
            return json.dumps({"success": False, "error": detalle}, ensure_ascii=False)
        chat_id = nuevo.get("id")
        creado = True
    enviado = json.loads(_send_chat_message_tool({"chat_id": chat_id, "message": message, "file_path": file_path}))
    if not enviado.get("success"):
        return json.dumps(
            {"success": False, "error": enviado.get("error"), "chat_id": chat_id, "chat_creado": creado},
            ensure_ascii=False,
        )
    consumida = _load_previews()
    consumida.pop(fingerprint, None)
    _save_previews(consumida)
    return json.dumps(
        {"success": True, "enviado_a": para, "chat_id": chat_id, "chat_creado": creado, "id": enviado.get("id")},
        ensure_ascii=False,
    )


# ── Plugin registration ───────────────────────────────────────────────────────


def register(ctx):
    ctx.register_tool(
        name="teams_list_teams",
        toolset=TOOLSET,
        schema={
            "name": "teams_list_teams",
            "description": "Lista los equipos de Microsoft Teams a los que pertenece el usuario.",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_list_teams_tool,
        description="Listar equipos de Teams",
        emoji="👥",
    )
    ctx.register_tool(
        name="teams_list_chats",
        toolset=TOOLSET,
        schema={
            "name": "teams_list_chats",
            "description": "Lista los chats recientes del usuario en Microsoft Teams.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Número de chats (máx 50, default 20)"},
                },
            },
        },
        handler=_list_chats_tool,
        description="Listar chats de Teams",
        emoji="💬",
    )
    ctx.register_tool(
        name="teams_get_chat_messages",
        toolset=TOOLSET,
        schema={
            "name": "teams_get_chat_messages",
            "description": "Obtiene los mensajes de un chat de Teams por su ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "ID del chat"},
                    "count": {"type": "integer", "description": "Número de mensajes (máx 50, default 20)"},
                },
                "required": ["chat_id"],
            },
        },
        handler=_get_chat_messages_tool,
        description="Obtener mensajes de un chat Teams",
        emoji="📨",
    )
    ctx.register_tool(
        name="teams_send_to_person",
        toolset=TOOLSET,
        schema={
            "name": "teams_send_to_person",
            "description": (
                "Envía un mensaje de Teams a UNA PERSONA por su correo, usando la cuenta del usuario: encuentra el chat "
                "1:1 con ella o lo crea si nunca hablaron. SIEMPRE confirma antes con el usuario a quién y qué texto: la "
                "primera llamada devuelve una vista previa sin enviar; solo con aprobado_por_el_usuario=true envía. "
                "Si solo tienes el nombre, busca el correo con ms365_find_person y confírmalo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Correo exacto de la persona."},
                    "message": {"type": "string", "description": "Texto del mensaje. Opcional si se envía file_path."},
                    "file_path": {"type": "string", "description": "Ruta local de un archivo a adjuntar (máx 4 MB)."},
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true SOLO si el usuario ya vio la vista previa y dijo que sí.",
                    },
                },
                "required": ["email"],
            },
        },
        handler=_send_to_person_tool,
        description="Enviar un mensaje de Teams a una persona por correo (con aprobación)",
        emoji="💬",
    )
    ctx.register_tool(
        name="teams_send_chat_message",
        toolset=TOOLSET,
        schema={
            "name": "teams_send_chat_message",
            "description": (
                "Envía un mensaje a un chat de Microsoft Teams, opcionalmente con un "
                "archivo adjunto real (PDF, imagen, etc.). Para adjuntar un archivo que "
                "generaste, pasa su ruta local en file_path — el archivo se sube a "
                "OneDrive y se adjunta como tarjeta de archivo real en el chat. "
                "NO escribas una línea 'MEDIA:/ruta/archivo.ext' dentro de message: esa "
                "convención no aplica a Teams y el texto se enviaría tal cual, sin "
                "adjuntar nada."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "ID del chat de Teams"},
                    "message": {
                        "type": "string",
                        "description": "Texto del mensaje. Opcional si se envía file_path.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Ruta local del archivo a adjuntar (máx 4 MB). Se sube a OneDrive "
                            "y se entrega como adjunto real, no como texto."
                        ),
                    },
                },
                "required": ["chat_id"],
            },
        },
        handler=_send_chat_message_tool,
        description="Enviar mensaje (y opcionalmente un archivo) en chat Teams",
        emoji="📤",
    )
