from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOOLSET = "hermeshq_ms365_calendar"


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
        headers={
            "X-HermesHQ-Agent-ID": agent_id,
            "X-HermesHQ-Agent-Token": agent_token,
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


def _graph(method: str, path: str, access_token: str, payload: dict | None = None) -> dict:
    url = (path if path.startswith("https://") else f"{GRAPH_BASE}{path}").replace(" ", "%20")
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method.upper(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
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
            "itemNotFound": "Evento no encontrado (404).",
            "ActivityLimitReached": f"Límite de peticiones alcanzado. Reintenta en {retry_after or '60'} segundos.",
        }
        friendly = _FRIENDLY.get(code) or (f"Error {exc.code}: {msg}" if msg else f"Error HTTP {exc.code}")
        result: dict = {"error": friendly, "_graph_code": code}
        if retry_after:
            result["retry_after"] = retry_after
        return result


def _auth_error(detail: str) -> str:
    return json.dumps(
        {
            "success": False,
            "error": f"No se pudo obtener token M365: {detail}. Verifica que el usuario haya conectado su cuenta Microsoft 365 en Mi cuenta.",
        }
    )


# ── Tool handlers ─────────────────────────────────────────────────────────────


def _list_events_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    count = min(int(args.get("count") or 10), 50)
    # Default: next 7 days
    now = datetime.now(UTC)
    start = args.get("start") or now.isoformat()
    end = args.get("end") or (now + timedelta(days=7)).isoformat()
    fields = "id,subject,start,end,location,organizer,isAllDay,bodyPreview,webLink"
    path = f"/me/calendarView?startDateTime={start}&endDateTime={end}&$top={count}&$select={fields}&$orderby=start/dateTime"
    result = _graph("GET", path, token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    events = result.get("value", [])
    return json.dumps({"success": True, "count": len(events), "events": events}, ensure_ascii=False)


def _get_event_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    event_id = str(args.get("event_id") or "").strip()
    if not event_id:
        return json.dumps({"success": False, "error": "Se requiere event_id."})
    result = _graph("GET", f"/me/events/{event_id}", token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    return json.dumps({"success": True, "event": result}, ensure_ascii=False)


def _create_event_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    subject = str(args.get("subject") or "").strip()
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    if not subject or not start or not end:
        return json.dumps({"success": False, "error": "Se requieren: subject, start, end (ISO 8601)."})
    timezone_str = str(args.get("timezone") or "UTC")
    body_content = str(args.get("body") or "")
    location = str(args.get("location") or "").strip()
    attendees_raw = args.get("attendees") or []
    attendees = [
        {"emailAddress": {"address": a.strip()}, "type": "required"}
        for a in (attendees_raw if isinstance(attendees_raw, list) else [attendees_raw])
        if a.strip()
    ]
    payload: dict = {
        "subject": subject,
        "start": {"dateTime": start, "timeZone": timezone_str},
        "end": {"dateTime": end, "timeZone": timezone_str},
    }
    if body_content:
        payload["body"] = {"contentType": "HTML", "content": body_content}
    if location:
        payload["location"] = {"displayName": location}
    if attendees:
        payload["attendees"] = attendees
    result = _graph("POST", "/me/events", token, payload)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    return json.dumps({"success": True, "event": result}, ensure_ascii=False)


def _update_event_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    event_id = str(args.get("event_id") or "").strip()
    if not event_id:
        return json.dumps({"success": False, "error": "Se requiere event_id."})
    patch: dict = {}
    if args.get("subject"):
        patch["subject"] = str(args["subject"]).strip()
    if args.get("start"):
        patch["start"] = {"dateTime": str(args["start"]), "timeZone": str(args.get("timezone") or "UTC")}
    if args.get("end"):
        patch["end"] = {"dateTime": str(args["end"]), "timeZone": str(args.get("timezone") or "UTC")}
    if args.get("body"):
        patch["body"] = {"contentType": "HTML", "content": str(args["body"])}
    if args.get("location"):
        patch["location"] = {"displayName": str(args["location"])}
    if not patch:
        return json.dumps({"success": False, "error": "No se proporcionaron campos para actualizar."})
    result = _graph("PATCH", f"/me/events/{event_id}", token, patch)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    return json.dumps({"success": True, "event": result}, ensure_ascii=False)


def _delete_event_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    event_id = str(args.get("event_id") or "").strip()
    if not event_id:
        return json.dumps({"success": False, "error": "Se requiere event_id."})
    result = _graph("DELETE", f"/me/events/{event_id}", token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    return json.dumps({"success": True, "message": "Evento eliminado correctamente."})


# ── Plugin registration ───────────────────────────────────────────────────────


def register(ctx):
    ctx.register_tool(
        name="ms365_calendar_list_events",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_list_events",
            "description": "Lista eventos del calendario Microsoft 365 del usuario en un rango de fechas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Inicio del rango en ISO 8601 (default: ahora)"},
                    "end": {"type": "string", "description": "Fin del rango en ISO 8601 (default: +7 días)"},
                    "count": {"type": "integer", "description": "Número de eventos (máx 50, default 10)"},
                    "timezone": {"type": "string", "description": "Zona horaria (default: UTC)"},
                },
            },
        },
        handler=_list_events_tool,
        description="Listar eventos de calendario M365",
        emoji="📅",
    )
    ctx.register_tool(
        name="ms365_calendar_get_event",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_get_event",
            "description": "Obtiene el detalle completo de un evento del calendario M365 por su ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "ID del evento"},
                },
                "required": ["event_id"],
            },
        },
        handler=_get_event_tool,
        description="Obtener evento de calendario M365",
        emoji="🗓️",
    )
    ctx.register_tool(
        name="ms365_calendar_create_event",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_create_event",
            "description": "Crea un nuevo evento en el calendario Microsoft 365 del usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Título del evento"},
                    "start": {"type": "string", "description": "Fecha/hora de inicio en ISO 8601"},
                    "end": {"type": "string", "description": "Fecha/hora de fin en ISO 8601"},
                    "timezone": {"type": "string", "description": "Zona horaria (default: UTC)"},
                    "body": {"type": "string", "description": "Descripción del evento (HTML)"},
                    "location": {"type": "string", "description": "Ubicación del evento"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de emails de asistentes",
                    },
                },
                "required": ["subject", "start", "end"],
            },
        },
        handler=_create_event_tool,
        description="Crear evento en calendario M365",
        emoji="➕",
    )
    ctx.register_tool(
        name="ms365_calendar_update_event",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_update_event",
            "description": "Actualiza un evento existente en el calendario M365.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "ID del evento a actualizar"},
                    "subject": {"type": "string", "description": "Nuevo título"},
                    "start": {"type": "string", "description": "Nueva fecha/hora de inicio ISO 8601"},
                    "end": {"type": "string", "description": "Nueva fecha/hora de fin ISO 8601"},
                    "timezone": {"type": "string", "description": "Zona horaria"},
                    "body": {"type": "string", "description": "Nueva descripción"},
                    "location": {"type": "string", "description": "Nueva ubicación"},
                },
                "required": ["event_id"],
            },
        },
        handler=_update_event_tool,
        description="Actualizar evento en calendario M365",
        emoji="✏️",
    )
    ctx.register_tool(
        name="ms365_calendar_delete_event",
        toolset=TOOLSET,
        schema={
            "name": "ms365_calendar_delete_event",
            "description": "Elimina un evento del calendario Microsoft 365 del usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "ID del evento a eliminar"},
                },
                "required": ["event_id"],
            },
        },
        handler=_delete_event_tool,
        description="Eliminar evento de calendario M365",
        emoji="🗑️",
    )
