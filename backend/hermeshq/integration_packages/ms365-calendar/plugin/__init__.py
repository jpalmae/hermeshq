from __future__ import annotations

import json
import os
import re
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


def _encode_url(url: str) -> str:
    return urllib.parse.quote(url, safe="%:/?#[]@!$&'()*+,;=~")


def _graph(method: str, path: str, access_token: str, payload: dict | None = None) -> dict:
    url = _encode_url(path if path.startswith("https://") else f"{GRAPH_BASE}{path}")
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


def _utc_stamp(value, label: str) -> tuple[str | None, str | None]:
    text = (
        str(value or "").strip().replace(" ", "+")
        if re.search(r"\d \d\d:\d\d$", str(value or "").strip())
        else str(value or "").strip()
    )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, f"{label} debe ser una fecha ISO 8601, por ejemplo 2026-10-08T13:00:00Z."
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), None


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
    start, start_error = _utc_stamp(args.get("start") or now.isoformat(), "start")
    end, end_error = _utc_stamp(args.get("end") or (now + timedelta(days=7)).isoformat(), "end")
    if start_error or end_error:
        return json.dumps({"success": False, "error": start_error or end_error}, ensure_ascii=False)
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


def _split_attendees(value) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    addresses: list[str] = []
    for item in items:
        for part in re.split(r"[;,\s]+", str(item)):
            part = part.strip().strip("<>\"'")
            if "@" in part and part.lower() not in (a.lower() for a in addresses):
                addresses.append(part)
    return addresses


def _truthy(value) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "si", "sí", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
        return None
    return bool(value)


def _teams_fields(args: dict) -> dict:
    wanted = _truthy(args.get("online_meeting"))
    if wanted is None:
        return {}
    if wanted:
        return {"isOnlineMeeting": True, "onlineMeetingProvider": "teamsForBusiness"}
    return {"isOnlineMeeting": False}


_IMPORTANCE = {"low", "normal", "high"}
_SHOW_AS = {"free", "tentative", "busy", "oof", "workingelsewhere"}


def _extra_fields(args: dict) -> tuple[dict, str | None]:
    fields: dict = {}
    if args.get("importance") is not None:
        value = str(args["importance"]).strip().lower()
        if value not in _IMPORTANCE:
            return {}, "importance debe ser low, normal o high."
        fields["importance"] = value
    if args.get("show_as") is not None:
        value = str(args["show_as"]).strip().lower()
        if value not in _SHOW_AS:
            return {}, "show_as debe ser free, tentative, busy, oof o workingElsewhere."
        fields["showAs"] = "workingElsewhere" if value == "workingelsewhere" else value
    if args.get("reminder_minutes") is not None:
        try:
            fields["reminderMinutesBeforeStart"] = max(0, min(int(args["reminder_minutes"]), 20160))
        except (TypeError, ValueError):
            return {}, "reminder_minutes debe ser un número de minutos."
        fields["isReminderOn"] = True
    all_day = _truthy(args.get("all_day"))
    if all_day is not None:
        fields["isAllDay"] = all_day
    if args.get("categories") is not None:
        raw = args["categories"]
        items = raw if isinstance(raw, list) else re.split(r"[;,]", str(raw))
        fields["categories"] = [str(c).strip() for c in items if str(c).strip()]
    return fields, None


def _parse_graph_datetime(value: str) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _event_response(result: dict) -> str:
    online = result.get("onlineMeeting") or {}
    out: dict = {"success": True, "event": result}
    if online.get("joinUrl"):
        out["teams_join_url"] = online["joinUrl"]
    return json.dumps(out, ensure_ascii=False)


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
        {"emailAddress": {"address": address}, "type": "required"} for address in _split_attendees(attendees_raw)
    ]
    attendees += [
        {"emailAddress": {"address": address}, "type": "optional"}
        for address in _split_attendees(args.get("optional_attendees"))
        if address.lower() not in {a["emailAddress"]["address"].lower() for a in attendees}
    ]
    extra, extra_error = _extra_fields(args)
    if extra_error:
        return json.dumps({"success": False, "error": extra_error}, ensure_ascii=False)
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
    payload.update(_teams_fields(args))
    payload.update(extra)
    result = _graph("POST", "/me/events", token, payload)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    return _event_response(result)


def _attendee_entry(address: str, existing: dict | None = None) -> dict:
    if existing:
        return existing
    return {"emailAddress": {"address": address}, "type": "required"}


def _build_attendee_patch(args: dict, current: list[dict] | None) -> list[dict] | None:
    full = args.get("attendees")
    add = _split_attendees(args.get("add_attendees"))
    add_optional = _split_attendees(args.get("add_optional_attendees"))
    remove = {a.lower() for a in _split_attendees(args.get("remove_attendees"))}
    if full is None and not add and not add_optional and not remove:
        return None
    if full is not None:
        base = [_attendee_entry(a) for a in _split_attendees(full)]
        if current:
            known = {((c.get("emailAddress") or {}).get("address") or "").lower(): c for c in current}
            base = [
                _attendee_entry(b["emailAddress"]["address"], known.get(b["emailAddress"]["address"].lower()))
                for b in base
            ]
    else:
        base = list(current or [])
    present = {((b.get("emailAddress") or {}).get("address") or "").lower() for b in base}
    for address in add:
        if address.lower() not in present:
            base.append(_attendee_entry(address))
            present.add(address.lower())
    for address in add_optional:
        if address.lower() not in present:
            base.append({"emailAddress": {"address": address}, "type": "optional"})
            present.add(address.lower())
    return [b for b in base if ((b.get("emailAddress") or {}).get("address") or "").lower() not in remove]


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
    timezone_str = str(args.get("timezone") or "UTC")
    if args.get("subject"):
        patch["subject"] = str(args["subject"]).strip()
    if args.get("end"):
        patch["end"] = {"dateTime": str(args["end"]), "timeZone": timezone_str}
    if args.get("start"):
        patch["start"] = {"dateTime": str(args["start"]), "timeZone": timezone_str}
        duration = None
        if args.get("duration_minutes") is not None:
            try:
                duration = timedelta(minutes=max(1, int(args["duration_minutes"])))
            except (TypeError, ValueError):
                return json.dumps({"success": False, "error": "duration_minutes debe ser un número."})
        elif not args.get("end"):
            current = _graph("GET", f"/me/events/{event_id}?$select=start,end", token)
            if "error" in current:
                return json.dumps({"success": False, "error": current["error"]})
            old_start = _parse_graph_datetime((current.get("start") or {}).get("dateTime"))
            old_end = _parse_graph_datetime((current.get("end") or {}).get("dateTime"))
            if old_start and old_end and old_end > old_start:
                duration = old_end - old_start
        new_start = _parse_graph_datetime(str(args["start"]))
        if duration is not None and new_start is not None and not args.get("end"):
            zone = timezone_str
            if new_start.tzinfo is not None:
                new_start = new_start.astimezone(UTC)
                zone = "UTC"
            patch["start"] = {"dateTime": new_start.replace(tzinfo=None).isoformat(), "timeZone": zone}
            patch["end"] = {"dateTime": (new_start + duration).replace(tzinfo=None).isoformat(), "timeZone": zone}
    elif args.get("duration_minutes") is not None:
        current = _graph("GET", f"/me/events/{event_id}?$select=start", token)
        if "error" in current:
            return json.dumps({"success": False, "error": current["error"]})
        cur_start = current.get("start") or {}
        base = _parse_graph_datetime(cur_start.get("dateTime"))
        try:
            minutes = max(1, int(args["duration_minutes"]))
        except (TypeError, ValueError):
            return json.dumps({"success": False, "error": "duration_minutes debe ser un número."})
        if base is None:
            return json.dumps({"success": False, "error": "No se pudo leer la hora de inicio actual del evento."})
        patch["end"] = {
            "dateTime": (base + timedelta(minutes=minutes)).replace(tzinfo=None).isoformat(),
            "timeZone": cur_start.get("timeZone") or "UTC",
        }
    if args.get("body") is not None:
        patch["body"] = {"contentType": "HTML", "content": str(args["body"])}
    if args.get("location") is not None:
        patch["location"] = {"displayName": str(args["location"])}
    extra, extra_error = _extra_fields(args)
    if extra_error:
        return json.dumps({"success": False, "error": extra_error}, ensure_ascii=False)
    patch.update(extra)
    patch.update(_teams_fields(args))
    wants_attendees = (
        args.get("attendees") is not None
        or args.get("add_attendees") is not None
        or args.get("add_optional_attendees") is not None
        or args.get("remove_attendees")
    )
    if wants_attendees:
        existing = _graph("GET", f"/me/events/{event_id}?$select=attendees", token)
        if "error" in existing:
            return json.dumps({"success": False, "error": existing["error"]})
        current = existing.get("attendees") or []
        attendees = _build_attendee_patch(args, current)
        if attendees is not None:
            patch["attendees"] = attendees
    if not patch:
        return json.dumps({"success": False, "error": "No se proporcionaron campos para actualizar."})
    result = _graph("PATCH", f"/me/events/{event_id}", token, patch)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    return _event_response(result)


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
                    "online_meeting": {
                        "type": "boolean",
                        "description": (
                            "true para habilitar una reunión de Microsoft Teams en el evento. "
                            "La respuesta trae teams_join_url con el enlace para unirse."
                        ),
                    },
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de emails de asistentes (obligatorios)",
                    },
                    "optional_attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de emails de asistentes opcionales",
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
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Lista COMPLETA de asistentes que deben quedar (reemplaza la actual; los omitidos "
                            "reciben cancelación). Prefiere add_attendees / remove_attendees para no perder a nadie."
                        ),
                    },
                    "add_attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Emails a AGREGAR a los asistentes actuales (conserva a los que ya están)",
                    },
                    "add_optional_attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Emails a AGREGAR como asistentes opcionales",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": (
                            "Nueva duración en minutos. Con start nuevo y sin end, se conserva la duración "
                            "actual salvo que indiques esta."
                        ),
                    },
                    "remove_attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Emails a QUITAR de los asistentes actuales (reciben cancelación)",
                    },
                    "online_meeting": {
                        "type": "boolean",
                        "description": (
                            "true para habilitar una reunión de Microsoft Teams en el evento. "
                            "La respuesta trae teams_join_url con el enlace para unirse."
                        ),
                    },
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
