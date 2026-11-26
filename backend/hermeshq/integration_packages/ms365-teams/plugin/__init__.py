from __future__ import annotations

import base64
import hashlib
import html as _html
import json
import mimetypes
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOOLSET = "hermeshq_ms365_teams"

# Graph's simple PUT .../content upload only supports files up to 4 MiB;
# larger files need a chunked upload session, which we don't implement here.
_MAX_SIMPLE_UPLOAD_BYTES = 4 * 1024 * 1024
_UPLOAD_FOLDER = "HermesHQ Uploads"


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
    return None


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
        headers={
            "X-HermesHQ-Agent-ID": agent_id,
            "X-HermesHQ-Agent-Token": agent_token,
            "X-HermesHQ-Attempt-ID": os.environ.get("HERMESHQ_ATTEMPT_ID", ""),
            "X-HermesHQ-Lease-Generation": os.environ.get("HERMESHQ_LEASE_GENERATION", ""),
        },
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


BROKER_TOKEN = "__broker__"


def _central_activa() -> bool:
    return os.environ.get("HERMESHQ_APROBACION_CENTRAL", "").strip() == "1"


def _cabeceras_control() -> dict:
    return {
        "X-HermesHQ-Agent-ID": os.environ.get("HERMESHQ_AGENT_ID", ""),
        "X-HermesHQ-Agent-Token": os.environ.get("HERMESHQ_AGENT_TOKEN", ""),
        "X-HermesHQ-Attempt-ID": os.environ.get("HERMESHQ_ATTEMPT_ID", ""),
        "X-HermesHQ-Lease-Generation": os.environ.get("HERMESHQ_LEASE_GENERATION", ""),
        "Content-Type": "application/json",
    }


def _llamar_control(metodo: str, ruta: str, payload: dict | None = None, timeout: int = 60) -> dict:
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    if not base_url or not os.environ.get("HERMESHQ_AGENT_TOKEN"):
        return {"ok": False, "codigo": "sin_control", "error": "HermesHQ internal control no configurado"}
    datos = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{base_url}{ruta}", data=datos, method=metodo, headers=_cabeceras_control())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, **json.loads(resp.read().decode("utf-8") or "{}")}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detalle = json.loads(raw).get("detail", raw)
        except (json.JSONDecodeError, ValueError, AttributeError):
            detalle = raw
        if isinstance(detalle, dict):
            codigo = detalle.get("motivo") or detalle.get("codigo") or "error"
            return {"ok": False, "codigo": codigo, "error": detalle.get("error") or codigo}
        return {"ok": False, "codigo": "error", "error": str(detalle)[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "codigo": "sin_respuesta", "error": f"{type(exc).__name__}: {exc}"}


def _token_herramienta(user_id: str) -> tuple[str | None, str]:
    if _central_activa():
        if not os.environ.get("HERMESHQ_INTERNAL_API_URL") or not _current_task_id():
            return None, "HermesHQ internal control no configurado o sin tarea activa."
        return BROKER_TOKEN, ""
    return _get_m365_token(user_id)


def _broker_graph(method: str, path: str, payload: dict | None = None) -> dict:
    respuesta = _llamar_control(
        "POST",
        "/control/m365/graph",
        {
            "tarea_id": _current_task_id(),
            "metodo": method.upper(),
            "ruta": path,
            **({"cuerpo_json": payload} if payload is not None else {}),
        },
    )
    if not respuesta.get("ok"):
        return {
            "error": f"HermesHQ no autorizó la operación ({respuesta.get('codigo')}).",
            "_graph_code": respuesta.get("codigo"),
        }
    return respuesta.get("json") or {}


def _ya_ejecutada_respuesta(r: dict) -> str:
    return json.dumps(
        {
            "success": True,
            "ya_ejecutada": True,
            "resultado": r.get("resultado") or {},
            "nota": "Esta acción ya se ejecutó con la aprobación del usuario; el resultado es el registrado. No se repitió.",
        },
        ensure_ascii=False,
    )


def _escritura_central(
    args: dict,
    metodo: str,
    ruta: str,
    payload: dict | None,
    herramienta: str,
    tipo: str,
    extra: dict | None = None,
    archivo_b64: str | None = None,
):
    contenido = {"metodo": metodo, "ruta": ruta, "cuerpo": payload or {}, **(extra or {})}
    aprobacion_id = str(args.get("aprobacion_id") or "").strip()
    if aprobacion_id:
        r = _llamar_control(
            "POST",
            "/control/m365/accion/ejecutar",
            {
                "tarea_id": _current_task_id(),
                "aprobacion_id": aprobacion_id,
                "contenido": contenido,
                **({"archivo_b64": archivo_b64} if archivo_b64 else {}),
            },
            timeout=90,
        )
        if r.get("ok"):
            if r.get("ya_ejecutada"):
                return _ya_ejecutada_respuesta(r)
            return r.get("resultado") or {}
        codigo = r.get("codigo")
        if codigo in ("ya_ejecutada", "accion_ya_ejecutada"):
            return json.dumps(
                {
                    "success": False,
                    "ya_ejecutada": True,
                    "error": "Esa acción ya se ejecutó con esta aprobación: no la repetí.",
                },
                ensure_ascii=False,
            )
        if codigo in ("conciliacion_manual_requerida", "conciliacion_prematura"):
            return json.dumps(
                {
                    "success": False,
                    "estado": "desconocido",
                    "codigo": codigo,
                    "error": "No sé todavía si se aplicó y no lo repito. "
                    + (
                        "Hay que revisarlo a mano en Outlook/Teams."
                        if codigo == "conciliacion_manual_requerida"
                        else "Vuelve a intentarlo en unos minutos: primero se verifica en Microsoft 365."
                    ),
                },
                ensure_ascii=False,
            )
        if codigo in ("resultado_incierto", "sin_respuesta", "conciliacion_requerida"):
            return json.dumps(
                {
                    "success": False,
                    "estado": "desconocido",
                    "codigo": codigo,
                    "error": "No sé si se aplicó. No la repito: hay que conciliarla primero.",
                },
                ensure_ascii=False,
            )
        if codigo in ("rechazada", "pendiente"):
            return json.dumps(
                {
                    "success": False,
                    "codigo": codigo,
                    "error": "El usuario rechazó la acción."
                    if codigo == "rechazada"
                    else "El usuario todavía no la aprueba.",
                },
                ensure_ascii=False,
            )
        if codigo == "fallida":
            return json.dumps(
                {
                    "success": False,
                    "codigo": codigo,
                    "error": f"El mensaje aprobado NO se envió. {r.get('error') or ''} No se repitió. "
                    "Para intentarlo otra vez hay que pedir una aprobación nueva.",
                },
                ensure_ascii=False,
            )
        if codigo not in ("huella_distinta", "expirada"):
            return json.dumps(
                {"success": False, "codigo": codigo, "error": r.get("error") or codigo}, ensure_ascii=False
            )
    creada = _llamar_control(
        "POST",
        "/control/aprobaciones",
        {
            "task_id": _current_task_id(),
            "tipo": tipo,
            "contenido": contenido,
            "parametros": {
                "herramienta": herramienta,
                "args": {k: v for k, v in args.items() if k not in ("aprobado_por_el_usuario", "aprobacion_id")},
            },
        },
    )
    if not creada.get("ok") or not creada.get("id"):
        return json.dumps(
            {"success": False, "codigo": creada.get("codigo"), "error": creada.get("error")}, ensure_ascii=False
        )
    if creada.get("estado") == "ejecutada" and not args.get("_reintento_aprobada"):
        return _escritura_central(
            dict(args, aprobacion_id=creada["id"], _reintento_aprobada=True),
            metodo,
            ruta,
            payload,
            herramienta,
            tipo,
            extra,
            archivo_b64,
        )
    if creada.get("estado") == "ejecutando":
        return json.dumps(
            {
                "success": False,
                "estado": "desconocido",
                "aprobacion_id": creada["id"],
                "error": "Esta acción ya está en curso o pendiente de conciliación. No la repito.",
            },
            ensure_ascii=False,
        )
    if creada.get("estado") == "aprobada" and not args.get("_reintento_aprobada"):
        return _escritura_central(
            dict(args, aprobacion_id=creada["id"], _reintento_aprobada=True),
            metodo,
            ruta,
            payload,
            herramienta,
            tipo,
            extra,
            archivo_b64,
        )
    salida = {
        "success": False,
        "requiere_aprobacion": True,
        "aprobacion_id": creada["id"],
        "borrador": creada.get("vista_previa"),
        "siguiente": "Muestra el campo 'borrador' TAL CUAL y pregunta si lo apruebas. Solo el usuario aprueba. "
        "Después vuelve a llamar con aprobacion_id y exactamente los mismos datos.",
    }
    if aprobacion_id:
        salida["error"] = "La acción cambió o la aprobación anterior ya no vale: se pidió una aprobación nueva."
    if creada.get("tarjeta"):
        salida["tarjeta_aprobacion"] = creada["tarjeta"]
    return json.dumps(salida, ensure_ascii=False)


def _encode_url(url: str) -> str:
    return urllib.parse.quote(url, safe="%:/?#[]@!$&'()*+,;=~")


def _graph(method: str, path: str, access_token: str, payload: dict | None = None) -> dict:
    if access_token == BROKER_TOKEN:
        return _broker_graph(method, path, payload)
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
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    result = _graph("GET", "/me/joinedTeams", token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    teams = result.get("value", [])
    simplified = [{"id": t.get("id"), "name": t.get("displayName"), "description": t.get("description")} for t in teams]
    return json.dumps({"success": True, "count": len(simplified), "teams": simplified}, ensure_ascii=False)


_ZONA_USUARIO = "America/Santiago"
_DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
_LECTURAS_DE_LA_TAREA: dict[str, int] = {}
_IDENTIDAD: dict = {}
_ETIQUETA_HTML = re.compile(r"<[^>]+>")
_NOTA_CHATS = (
    "Las horas ya están en hora de Chile: no las conviertas. pendiente: te_toca_responder = el último mensaje es de "
    "otra persona; esperas_respuesta = el último es tuyo y no te han respondido. leido: false = hay mensajes de otra "
    "persona que el usuario no ha abierto (eso es «no leído», distinto de «sin responder»); null = no se sabe. Para "
    "«chats sin responder» o «no leídos» usa esta lista tal cual: no abras cada chat. Si el usuario duda de una hora o "
    "de quién escribió, vuelve a llamar teams_list_chats; no corrijas de memoria."
)


def _leido(ultimo: dict | None, chat: dict, identidad: dict) -> bool | None:
    if not isinstance(ultimo, dict) or _es_propio(ultimo, identidad):
        return None if not isinstance(ultimo, dict) else True
    leido_hasta = ((chat or {}).get("viewpoint") or {}).get("lastMessageReadDateTime")
    creado = ultimo.get("createdDateTime")
    if not leido_hasta or not creado:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(leido_hasta).replace("Z", "+00:00")) >= datetime.fromisoformat(
            str(creado).replace("Z", "+00:00")
        )
    except ValueError:
        return None


def _hora_chile(iso: str | None) -> str | None:
    if not iso:
        return None
    from datetime import UTC, datetime, timedelta

    try:
        instante = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=UTC)
    try:
        from zoneinfo import ZoneInfo

        local = instante.astimezone(ZoneInfo(_ZONA_USUARIO))
    except Exception:  # noqa: BLE001
        local = instante.astimezone(UTC) - timedelta(hours=3)
    return f"{_DIAS[local.weekday()]} {local:%d-%m %H:%M}"


def _texto_plano(html: str | None, largo: int = 0) -> str:
    texto = _ETIQUETA_HTML.sub(" ", str(html or ""))
    texto = re.sub(r"\s+", " ", _html.unescape(texto)).strip()
    return texto if not largo or len(texto) <= largo else texto[: largo - 1] + "…"


def _identidad_propia(token: str) -> dict:
    if "nombre" not in _IDENTIDAD:
        yo = _graph("GET", "/me?$select=displayName,mail,userPrincipalName", token)
        if "error" in yo:
            yo = {}
        _IDENTIDAD["nombre"] = str(yo.get("displayName") or "").strip()
        _IDENTIDAD["correos"] = {
            str(v).strip().lower() for v in (yo.get("mail"), yo.get("userPrincipalName")) if v and str(v).strip()
        }
        _IDENTIDAD.setdefault("ids", set())
    return _IDENTIDAD


def _ids_propios_de_los_miembros(chats: list[dict], identidad: dict) -> set[str]:
    ids = set(identidad.get("ids") or ())
    for c in chats:
        for m in c.get("members") or []:
            correo = str(m.get("email") or "").strip().lower()
            if m.get("userId") and correo and correo in identidad.get("correos", set()):
                ids.add(str(m["userId"]))
    if not ids:
        directos = [c for c in chats if c.get("chatType") == "oneOnOne" and len(c.get("members") or []) == 2]
        comunes = None
        for c in directos:
            presentes = {str(m.get("userId")) for m in c.get("members") or [] if m.get("userId")}
            comunes = presentes if comunes is None else comunes & presentes
        if comunes and len(comunes) == 1 and len(directos) >= 2:
            ids = set(comunes)
    identidad["ids"] = ids
    return ids


def _es_propio(mensaje: dict, identidad: dict) -> bool:
    usuario = (mensaje.get("from") or {}).get("user") or {}
    if usuario.get("id") and str(usuario["id"]) in (identidad.get("ids") or set()):
        return True
    nombre = str(usuario.get("displayName") or "").strip()
    return bool(nombre) and nombre == identidad.get("nombre")


def _estado_del_chat(ultimo: dict | None, identidad: dict) -> str:
    if not isinstance(ultimo, dict) or ultimo.get("isDeleted") or (ultimo.get("messageType") or "message") != "message":
        return "sin_mensajes"
    remitente = ultimo.get("from") or {}
    if remitente.get("application") and not remitente.get("user"):
        return "bot"
    if not remitente.get("user"):
        return "sin_mensajes"
    return "esperas_respuesta" if _es_propio(ultimo, identidad) else "te_toca_responder"


def _lectura_repetida(clave: str) -> int:
    _LECTURAS_DE_LA_TAREA[clave] = _LECTURAS_DE_LA_TAREA.get(clave, 0) + 1
    return _LECTURAS_DE_LA_TAREA[clave] - 1


_AVISO_REPETIDA = "Ya hiciste esta misma lectura en esta tarea y el resultado es el mismo: usa el que ya tienes."


def _list_chats_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    count = min(int(args.get("count") or 20), 50)
    repetida = _lectura_repetida(f"chats:{count}")
    if repetida >= 2:
        return json.dumps({"success": False, "error": _AVISO_REPETIDA}, ensure_ascii=False)
    result = _graph("GET", f"/me/chats?$top={count}&$expand=members", token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    chats = result.get("value", [])
    recientes = _graph(
        "GET",
        f"/me/chats?$top={count}&$expand=lastMessagePreview&$orderby=lastMessagePreview/createdDateTime%20desc",
        token,
    )
    previas = {}
    vistas = {}
    orden = []
    if "error" not in recientes:
        for c in recientes.get("value", []) or []:
            if isinstance(c, dict) and c.get("id") and isinstance(c.get("lastMessagePreview"), dict):
                previas[c["id"]] = c["lastMessagePreview"]
                vistas[c["id"]] = c
                orden.append(c)
    por_id = {c.get("id"): c for c in chats if isinstance(c, dict)}
    sin_miembros = [c for c in orden if c["id"] not in por_id]
    for i, c in enumerate(sin_miembros):
        miembros = _graph("GET", f"/me/chats/{c['id']}/members", token) if i < 10 else {}
        por_id[c["id"]] = {**c, "members": (miembros.get("value") or []) if "error" not in miembros else []}
    identidad = _identidad_propia(token) if previas else {}
    if previas:
        _ids_propios_de_los_miembros(list(por_id.values()), identidad)
    ids_en_orden = [c["id"] for c in orden] + [i for i in por_id if i not in previas]
    simplified = []
    for chat_id in ids_en_orden[:count]:
        c = por_id.get(chat_id) or {}
        members = [m.get("displayName") for m in (c.get("members") or []) if m.get("displayName")]
        fila = {
            "id": chat_id,
            "topic": c.get("topic") or ", ".join(members[:3]),
            "type": c.get("chatType"),
            "members": members,
        }
        ultimo = previas.get(chat_id)
        if ultimo is not None:
            fila["ultimo_mensaje"] = {
                "de": "tú" if _es_propio(ultimo, identidad) else _message_sender(ultimo),
                "hora_chile": _hora_chile(ultimo.get("createdDateTime")),
                "texto": _texto_plano((ultimo.get("body") or {}).get("content"), 160),
            }
            fila["pendiente"] = _estado_del_chat(ultimo, identidad)
            fila["leido"] = _leido(ultimo, vistas.get(chat_id) or {}, identidad)
        simplified.append(fila)
    salida = {"success": True, "count": len(simplified), "chats": simplified}
    if previas:
        salida["resumen"] = {
            estado: sum(1 for f in simplified if f.get("pendiente") == estado)
            for estado in ("te_toca_responder", "esperas_respuesta")
        }
        salida["resumen"]["no_leidos"] = sum(1 for f in simplified if f.get("leido") is False)
        salida["nota"] = _NOTA_CHATS
    else:
        salida["nota"] = (
            "No se pudo leer el último mensaje de cada chat: ábrelos con teams_get_chat_messages (las horas vienen "
            "en hora de Chile)."
        )
    if repetida:
        salida["aviso"] = _AVISO_REPETIDA
    return json.dumps(salida, ensure_ascii=False)


def _message_sender(message: dict) -> str | None:
    sender = message.get("from") or {}
    for kind in ("user", "application", "device"):
        name = (sender.get(kind) or {}).get("displayName")
        if name:
            return name
    return None


def _get_chat_messages_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    chat_id = str(args.get("chat_id") or "").strip()
    if not chat_id:
        return json.dumps({"success": False, "error": "Se requiere chat_id."})
    count = min(int(args.get("count") or 20), 50)
    repetida = _lectura_repetida(f"mensajes:{chat_id}:{count}")
    if repetida >= 2:
        return json.dumps({"success": False, "error": _AVISO_REPETIDA}, ensure_ascii=False)
    result = _graph("GET", f"/me/chats/{chat_id}/messages?$top={count}", token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    messages = result.get("value", [])
    identidad = _identidad_propia(token)
    simplified = [
        {
            "id": m.get("id"),
            "from": _message_sender(m),
            "tuyo": _es_propio(m, identidad),
            "body": _texto_plano((m.get("body") or {}).get("content", "")),
            "hora_chile": _hora_chile(m.get("createdDateTime")),
        }
        for m in messages
    ]
    salida = {
        "success": True,
        "count": len(simplified),
        "messages": simplified,
        "nota": "Horas ya en hora de Chile (no las conviertas); tuyo=true es un mensaje del usuario.",
    }
    if repetida:
        salida["aviso"] = _AVISO_REPETIDA
    return json.dumps(salida, ensure_ascii=False)


_ARGS_CENTRAL: dict = {}


def _archivo_central(file_path: str) -> tuple[dict | None, str | None, str | None]:
    base = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()).resolve()
    candidato = Path(file_path)
    ruta = (candidato if candidato.is_absolute() else base / candidato).resolve()
    if ruta != base and base not in ruta.parents:
        return None, None, f"El archivo '{file_path}' está fuera del directorio de trabajo."
    if not ruta.is_file():
        return None, None, f"Archivo no encontrado: {file_path}"
    datos = ruta.read_bytes()
    if len(datos) > _MAX_SIMPLE_UPLOAD_BYTES:
        return (
            None,
            None,
            (
                f"El archivo pesa {len(datos) / (1024 * 1024):.1f} MB; el límite para adjuntar en Teams es "
                f"{_MAX_SIMPLE_UPLOAD_BYTES / (1024 * 1024):.0f} MB."
            ),
        )
    meta = {"nombre": ruta.name, "sha256": hashlib.sha256(datos).hexdigest(), "bytes": len(datos)}
    return meta, base64.b64encode(datos).decode("ascii"), None


def _marcar_en_curso(fingerprint: str) -> None:
    previews = _load_previews()
    entrada = previews.get(fingerprint) or {}
    previews[fingerprint] = {**entrada, "en_curso": time.time(), "ts": entrada.get("ts") or time.time()}
    _save_previews(previews)


def _graph_rechazo_firme(resultado: dict) -> bool:
    return bool(
        re.match(
            r"^(Error \d{3}|Sin permiso|Chat o elemento no encontrado|Límite de peticiones)",
            str(resultado.get("error") or ""),
        )
    )


def _desmarcar_en_curso(fingerprint: str) -> None:
    previews = _load_previews()
    if fingerprint in previews:
        previews[fingerprint].pop("en_curso", None)
        _save_previews(previews)


def _consumir(fingerprint: str) -> None:
    previews = _load_previews()
    previews.pop(fingerprint, None)
    _save_previews(previews)


def _mensaje_ya_salio(token: str, chat_id: str, message: str, desde: float) -> str:
    lista = _graph("GET", f"/me/chats/{chat_id}/messages?$top=15", token)
    if not isinstance(lista, dict) or "error" in lista:
        return "no_se_pudo"
    objetivo = _plano(message)
    identidad = _identidad_propia(token)
    yo = _graph("GET", "/me?$select=id", token)
    mi_id = str(yo.get("id") or "") if isinstance(yo, dict) else ""
    for m in lista.get("value") or []:
        if m.get("messageType") not in (None, "message"):
            continue
        de = ((m.get("from") or {}).get("user") or {}).get("id")
        if not (mi_id and str(de or "") == mi_id) and not _es_propio(m, identidad):
            continue
        creado = str(m.get("createdDateTime") or "")
        try:
            instante = datetime.fromisoformat(creado.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if instante < desde - 120:
            continue
        if _plano(_texto_plano((m.get("body") or {}).get("content"))) == objetivo and objetivo:
            return "salio"
    return "no_salio"


def _reintento_tras_corte(token: str, chat_id: str, message: str, file_path: str, fingerprint: str) -> str | None:
    entrada = _load_previews().get(fingerprint) or {}
    desde = float(entrada.get("en_curso") or 0)
    if not desde:
        return None
    estado = _mensaje_ya_salio(token, chat_id, message, desde) if not file_path or message else "no_se_pudo"
    if estado == "salio":
        _consumir(fingerprint)
        return json.dumps(
            {
                "success": True,
                "ya_enviado": True,
                "message": "Ese mensaje ya había salido en el intento anterior (el chat lo muestra); no lo envié otra vez.",
            },
            ensure_ascii=False,
        )
    if estado == "no_se_pudo":
        return json.dumps(
            {
                "success": False,
                "error": "El intento anterior se cortó y no pude revisar el chat para saber si el mensaje salió. No lo "
                "reenvío para no duplicarlo: pídele al usuario que mire el chat y diga si hay que enviarlo.",
            },
            ensure_ascii=False,
        )
    return None


def _post_chat_message(token: str, chat_id: str, message: str, file_path: str) -> str:
    if _central_activa():
        extra, archivo_b64 = None, None
        if file_path:
            meta, archivo_b64, error = _archivo_central(file_path)
            if error:
                return json.dumps({"success": False, "error": error}, ensure_ascii=False)
            extra = {"archivo": meta}
        result = _escritura_central(
            _ARGS_CENTRAL.get("args") or {},
            "POST",
            f"/me/chats/{chat_id}/messages",
            {"body": {"contentType": "text", "content": message}},
            _ARGS_CENTRAL.get("herramienta") or "send_chat",
            "graph.teams",
            extra,
            archivo_b64,
        )
        if isinstance(result, str):
            return result
        if "error" in result:
            return json.dumps({"success": False, "error": result["error"]})
        return json.dumps({"success": True, "message": "Mensaje enviado correctamente.", "id": result.get("id")})
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

    if _central_activa():
        result = _escritura_central(
            _ARGS_CENTRAL.get("args") or {},
            "POST",
            f"/me/chats/{chat_id}/messages",
            payload,
            _ARGS_CENTRAL.get("herramienta") or "send_chat",
            "graph.teams",
        )
        if isinstance(result, str):
            return result
    else:
        result = _graph("POST", f"/me/chats/{chat_id}/messages", token, payload)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    return json.dumps({"success": True, "message": "Mensaje enviado correctamente.", "id": result.get("id")})


def _plano(texto: str) -> str:
    sin_acentos = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", sin_acentos.lower())).strip()


def _nombre_del_chat(chat: dict) -> str:
    miembros = [m.get("displayName") for m in (chat.get("members") or []) if m.get("displayName")]
    return str(chat.get("topic") or ", ".join(miembros[:4]) or chat.get("id") or "").strip()


def _resolver_chat(token: str, chat_id: str, chat_nombre: str) -> tuple[str | None, str, str | None]:
    if chat_id:
        chat = _graph("GET", f"/me/chats/{chat_id}?$expand=members", token)
        if "error" not in chat and chat.get("id"):
            return str(chat["id"]), _nombre_del_chat(chat), None
        if not chat_nombre:
            return (
                None,
                "",
                f"El chat_id '{chat_id}' no existe entre los chats del usuario: no inventes IDs. Llama de nuevo con "
                "chat_nombre (el nombre del grupo o de la persona, p. ej. «Club de trabajo»).",
            )
    buscado = _plano(chat_nombre)
    if not buscado:
        return None, "", "Se requiere chat_nombre (nombre del grupo o de la persona) o un chat_id de teams_list_chats."
    lista = _graph("GET", "/me/chats?$top=50&$expand=members", token)
    if "error" in lista:
        return None, "", lista["error"]
    chats = [c for c in lista.get("value") or [] if isinstance(c, dict) and c.get("id")]
    palabras = set(buscado.split())

    def _calza(c: dict) -> bool:
        if c.get("topic"):
            return buscado in _plano(c["topic"])
        nombres = [_plano(m.get("displayName")) for m in c.get("members") or []]
        return any(buscado in n for n in nombres) or palabras <= set(" ".join(nombres).split())

    exactos = [c for c in chats if buscado in (_plano(c.get("topic") or ""), _plano(_nombre_del_chat(c)))]
    parciales = exactos or [c for c in chats if _calza(c)]
    if len(parciales) == 1:
        return str(parciales[0]["id"]), _nombre_del_chat(parciales[0]), None
    if not parciales:
        return None, "", f"No encontré un chat llamado «{chat_nombre}» entre los 50 más recientes del usuario."
    opciones = "; ".join(_nombre_del_chat(c) for c in parciales[:6])
    return None, "", f"Hay varios chats que calzan con «{chat_nombre}»: {opciones}. Pregunta al usuario cuál es."


def _send_chat_message_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    chat_id = str(args.get("chat_id") or "").strip()
    chat_nombre = str(args.get("chat_nombre") or "").strip()
    message = str(args.get("message") or "").strip()
    file_path = str(args.get("file_path") or "").strip()
    if (not chat_id and not chat_nombre) or (not message and not file_path):
        return json.dumps(
            {"success": False, "error": "Se requiere chat_nombre (o chat_id) y al menos message o file_path."}
        )
    if _modo_automatico():
        return _deny_automatico()
    chat_id, nombre_chat, error_chat = _resolver_chat(token, chat_id, chat_nombre)
    if error_chat:
        return json.dumps({"success": False, "error": error_chat}, ensure_ascii=False)
    if not args.get("aprobado_por_el_usuario") and str(args.get("approved_by_user") or "").lower() in ("true", "1"):
        args = {**args, "aprobado_por_el_usuario": True}
    for clave in [k for k in _LECTURAS_DE_LA_TAREA if k.startswith((f"mensajes:{chat_id}:", "chats:"))]:
        del _LECTURAS_DE_LA_TAREA[clave]
    if _central_activa():
        _ARGS_CENTRAL.clear()
        _ARGS_CENTRAL.update(args=args, herramienta="send_chat")
        return _post_chat_message(token, chat_id, message, file_path)
    fingerprint = _message_fingerprint(user_id, f"chat:{chat_id}", file_path, message)
    if not args.get("aprobado_por_el_usuario") or not _approval_is_real(fingerprint):
        error = _error_aprobacion(fingerprint)
        _remember_preview(fingerprint)
        resultado = {
            "success": False,
            "requiere_aprobacion": True,
            "vista_previa": {"chat": nombre_chat, "chat_id": chat_id, "mensaje": message, "archivo": file_path or None},
            "siguiente": f"Muéstrale al usuario esta vista previa (chat «{nombre_chat}» y el texto) y pregunta si lo "
            "envías; termina tu respuesta ahí y espera su siguiente mensaje. Cuando diga que sí, vuelve a llamar con "
            f"aprobado_por_el_usuario=true, chat_nombre=«{nombre_chat}» y EXACTAMENTE el mismo texto y adjunto (si "
            "cambias algo, es otra vista previa). No inventes chat_id.",
        }
        if args.get("aprobado_por_el_usuario"):
            resultado["error"] = error
        _registrar_pendiente("send_chat", {**args, "chat_id": chat_id, "chat_nombre": nombre_chat}, resultado)
        return json.dumps(resultado, ensure_ascii=False)
    previo = _reintento_tras_corte(token, chat_id, message, file_path, fingerprint)
    if previo is not None:
        return previo
    _marcar_en_curso(fingerprint)
    enviado = _post_chat_message(token, chat_id, message, file_path)
    resultado = json.loads(enviado)
    if resultado.get("success"):
        _consumir(fingerprint)
    elif "error" in resultado and _graph_rechazo_firme(resultado):
        _desmarcar_en_curso(fingerprint)
    return enviado


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


def _modo_automatico() -> bool:
    raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    if not raw:
        return False
    try:
        meta = json.loads(raw).get("metadata") or {}
    except (ValueError, AttributeError):
        return False
    return bool(meta.get("scheduled") or meta.get("background_job"))


def _deny_automatico() -> str:
    return json.dumps(
        {
            "success": False,
            "error": "No puedo enviar mensajes de Teams en modo automático (tarea programada o en segundo plano): "
            "no hay un usuario que apruebe la vista previa.",
        },
        ensure_ascii=False,
    )


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


_PALABRAS_APROBACION = {
    "si",
    "sip",
    "ok",
    "okay",
    "oka",
    "dale",
    "listo",
    "confirmo",
    "confirmado",
    "adelante",
    "perfecto",
    "correcto",
    "procede",
    "hazlo",
    "autorizo",
    "apruebo",
    "aprobado",
    "yes",
    "envia",
    "envialo",
    "enviala",
    "enviar",
    "enviale",
    "manda",
    "mandalo",
    "mandala",
    "mandale",
    "mandar",
}
_PALABRAS_RELLENO = {
    "de",
    "acuerdo",
    "asi",
    "tal",
    "cual",
    "ya",
    "por",
    "favor",
    "porfa",
    "nomas",
    "lo",
    "la",
    "le",
    "el",
    "los",
    "las",
    "ahora",
    "mensaje",
    "chat",
}
_PALABRAS_QUE_ANULAN = {
    "no",
    "nop",
    "nope",
    "nunca",
    "tampoco",
    "ni",
    "nada",
    "stop",
    "alto",
    "todavia",
    "aun",
    "mejor",
    "deten",
    "detente",
    "pero",
    "sino",
    "cambia",
    "cambialo",
    "cambiala",
    "corrige",
    "modifica",
    "error",
    "parar",
    "parale",
    "parala",
    "paralo",
    "parate",
}
_PREFIJOS_QUE_ANULAN = ("olvid", "cancel", "esper", "aguard", "anul", "descart", "dej", "frena")
_BLOQUE_RESPUESTA_CORTA = re.compile(r"(?s)\[Respuesta corta a tu último mensaje:.*?no a temas anteriores\.\]\s*")
_BLOQUES_DEL_SISTEMA = re.compile(
    r"(?s)\[(?:FORMATO PEDIDO EN ESTE MENSAJE|Hora actual|AVISO DEL SISTEMA|Archivos adjuntos|Archivos de turnos"
    r"|B[uú]squeda en SharePoint)[^\]]*(?:\]|$)"
)
_LINEA_ARCHIVO_PREVIO = re.compile(r"(?m)^\s*- .+ \(hace \d+ min\)\s*$")


def _mensaje_del_usuario() -> str:
    try:
        datos = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return ""
    texto = str((datos.get("mensaje") if isinstance(datos, dict) else "") or "")
    texto = _BLOQUE_RESPUESTA_CORTA.sub(" ", texto)
    texto = _BLOQUES_DEL_SISTEMA.sub(" ", texto)
    return _LINEA_ARCHIVO_PREVIO.sub(" ", texto).strip()


def _palabras_planas(texto: str) -> list[str]:
    return re.findall(r"[a-z]+", unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower())


def _anula(palabra: str) -> bool:
    for forma in {palabra, re.sub(r"(.)\1{2,}", r"\1", palabra), re.sub(r"(.)\1+", r"\1", palabra)}:
        if forma in _PALABRAS_QUE_ANULAN or forma.startswith(_PREFIJOS_QUE_ANULAN):
            return True
    return False


def _usuario_confirma(accion: frozenset = frozenset()) -> bool:
    texto = _mensaje_del_usuario()
    if not texto or len(texto) > 60 or "?" in texto or "¿" in texto:
        return False
    palabras = _palabras_planas(texto)
    if not palabras or any(_anula(p) for p in palabras if p not in accion):
        return False
    if not any(p in _PALABRAS_APROBACION for p in palabras):
        return False
    return all(p in _PALABRAS_APROBACION or p in _PALABRAS_RELLENO or p in accion for p in palabras)


def _huella_archivo(file_path: str) -> str:
    if not file_path:
        return ""
    try:
        return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
    except OSError:
        return "sin-archivo"


def _message_fingerprint(user_id: str, person_id: str, file_path: str, message: str) -> str:
    return hashlib.sha256(
        json.dumps([user_id, person_id, message, file_path, _huella_archivo(file_path)]).encode()
    ).hexdigest()


def _remember_preview(fingerprint: str) -> None:
    previews = _load_previews()
    anterior = previews.get(fingerprint) or {}
    previews[fingerprint] = {
        "task": _current_task_id(),
        "ts": time.time(),
        **({"en_curso": anterior["en_curso"]} if anterior.get("en_curso") else {}),
    }
    _save_previews(previews)


def _previa_de_otra_tarea(fingerprint: str) -> bool:
    entry = _load_previews().get(fingerprint)
    current = _current_task_id()
    return bool(entry and current and entry.get("task") and entry["task"] != current)


def _approval_is_real(fingerprint: str) -> bool:
    return _previa_de_otra_tarea(fingerprint) and _usuario_confirma()


def _error_aprobacion(fingerprint: str) -> str:
    if _previa_de_otra_tarea(fingerprint):
        return (
            "No se envió: el último mensaje del usuario no es una confirmación clara (no dijo «sí», o lo negó, "
            "canceló o pidió esperar). No lo envíes ni digas que se envió: pregúntale si lo envías."
        )
    return (
        "No se envió: esta vista previa exacta (destinatario, texto y adjunto) aún no se le mostró al usuario en un "
        "mensaje anterior. No puedes aprobar por él en la misma respuesta."
    )


def _send_to_person_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
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
    if _modo_automatico():
        return _deny_automatico()
    if _central_activa():
        chat_id = _find_direct_chat(token, str(persona.get("id"))) or f"directo:{persona.get('id')}"
        _ARGS_CENTRAL.clear()
        _ARGS_CENTRAL.update(args=args, herramienta="send_person")
        enviado = json.loads(_post_chat_message(token, chat_id, message, file_path))
        if enviado.get("success") is True or "requiere_aprobacion" in enviado:
            return json.dumps({**enviado, "para": para, "chat_id": chat_id}, ensure_ascii=False)
        return json.dumps(enviado, ensure_ascii=False)
    fingerprint = _message_fingerprint(user_id, str(persona.get("id")), file_path, message)
    if not args.get("aprobado_por_el_usuario") or not _approval_is_real(fingerprint):
        error = _error_aprobacion(fingerprint)
        _remember_preview(fingerprint)
        resultado = {
            "success": False,
            "requiere_aprobacion": True,
            "vista_previa": {"para": para, "mensaje": message, "archivo": file_path or None},
            "siguiente": "Muéstrale al usuario esta vista previa (a quién y qué texto) y pregunta si lo envías; "
            "termina tu respuesta ahí y espera su siguiente mensaje. Cuando diga que sí, vuelve a llamar con "
            "aprobado_por_el_usuario=true y EXACTAMENTE la misma persona, texto y adjunto (si cambias algo, es otra "
            "vista previa).",
        }
        if args.get("aprobado_por_el_usuario"):
            resultado["error"] = error
        _registrar_pendiente("send_person", args, resultado)
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
    previo = _reintento_tras_corte(token, chat_id, message, file_path, fingerprint)
    if previo is not None:
        return previo
    _marcar_en_curso(fingerprint)
    enviado = json.loads(_post_chat_message(token, chat_id, message, file_path))
    if not enviado.get("success"):
        if _graph_rechazo_firme(enviado):
            _desmarcar_en_curso(fingerprint)
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


_PENDIENTE_TTL = 4 * 3600


def _pendiente_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".teams_pendiente.json"


def _payload_tarea() -> dict:
    try:
        datos = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return {}
    return datos if isinstance(datos, dict) else {}


def _conversacion_actual() -> str:
    meta = _payload_tarea().get("metadata") or {}
    if not isinstance(meta, dict):
        return ""
    for campo in ("thread_id", "public_session_id"):
        valor = str(meta.get(campo) or "").strip()
        if valor:
            return valor
    return ""


def _registrar_pendiente(herramienta: str, args: dict, resultado: dict) -> None:
    if not isinstance(resultado, dict) or not resultado.get("requiere_aprobacion"):
        return
    datos = {
        "herramienta": herramienta,
        "args": {k: v for k, v in args.items() if k not in ("aprobado_por_el_usuario", "approved_by_user")},
        "task": _current_task_id(),
        "conv": _conversacion_actual(),
        "ts": time.time(),
        "preview": resultado.get("vista_previa") or {},
    }
    try:
        ruta = _pendiente_path()
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta.with_suffix(".tmp")
        tmp.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, ruta)
    except OSError:
        pass


def _leer_pendiente() -> dict | None:
    try:
        datos = json.loads(_pendiente_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(datos, dict) or time.time() - float(datos.get("ts") or 0) > _PENDIENTE_TTL:
        return None
    return datos


def _borrar_pendiente() -> None:
    try:
        _pendiente_path().unlink()
    except OSError:
        pass


def _sin_espacios(texto) -> str:
    return re.sub(r"\s+", " ", str(texto or "")).strip().lower()


def _vista_mostrada(pendiente: dict) -> bool:
    anterior = _sin_espacios(_payload_tarea().get("respuesta_anterior"))
    mostrada = _sin_espacios(pendiente.get("mostrada"))
    return bool(anterior and mostrada and mostrada in anterior)


def ejecutar_aprobacion_pendiente() -> dict | None:
    if _modo_automatico() or _central_activa():
        return None
    pendiente = _leer_pendiente()
    if not pendiente or pendiente.get("task") == _current_task_id():
        return None
    if str(pendiente.get("conv") or "") != _conversacion_actual():
        return None
    acciones = {
        "send_chat": ("teams_send_chat_message", _send_chat_message_tool),
        "send_person": ("teams_send_to_person", _send_to_person_tool),
    }
    if pendiente.get("herramienta") not in acciones:
        return None
    if not _usuario_confirma() or not _vista_mostrada(pendiente):
        return None
    nombre, funcion = acciones[pendiente["herramienta"]]
    args = dict(pendiente.get("args") or {}, aprobado_por_el_usuario=True)
    try:
        resultado = json.loads(funcion(args))
    except Exception as exc:  # noqa: BLE001
        resultado = {"success": False, "error": f"Falló el envío por Teams: {exc}"}
    if not resultado.get("requiere_aprobacion"):
        _borrar_pendiente()
    return {"herramienta": nombre, "args": args, "resultado": resultado, "preview": pendiente.get("preview") or {}}


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
            "description": (
                "Lista los chats recientes de Teams, del más reciente al más antiguo, con el último mensaje de cada "
                "uno (quién, hora de Chile y texto) y si te toca responder o esperas respuesta. Úsala para «chats "
                "sin responder»: no abras cada chat."
            ),
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
                "adjuntar nada. La primera llamada devuelve una vista previa (chat y texto) SIN "
                "enviar; muéstrasela al usuario y solo con aprobado_por_el_usuario=true y los "
                "mismos datos, en un mensaje posterior, se envía."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_nombre": {
                        "type": "string",
                        "description": "Nombre del grupo o de la persona del chat (p. ej. «Club de trabajo»). Úsalo "
                        "siempre que no tengas el chat_id de un resultado de ESTE mensaje; nunca inventes un ID.",
                    },
                    "chat_id": {"type": "string", "description": "ID del chat, solo si viene de teams_list_chats"},
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
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true SOLO si el usuario ya vio la vista previa y dijo que sí.",
                    },
                },
            },
        },
        handler=_send_chat_message_tool,
        description="Enviar mensaje (y opcionalmente un archivo) en chat Teams",
        emoji="📤",
    )
