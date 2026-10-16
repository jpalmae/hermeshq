from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOOLSET = "hermeshq_ms365_mail"

_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_MAX_INLINE_IMAGES = 10
_MAX_INLINE_IMAGE_BYTES = 1_500_000
_MAX_INLINE_TOTAL_BYTES = 2_500_000
_CONTENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _load_inline_images(items) -> tuple[list[dict], str | None]:
    if not items:
        return [], None
    if not isinstance(items, list) or len(items) > _MAX_INLINE_IMAGES:
        return [], f"inline_images debe ser una lista de hasta {_MAX_INLINE_IMAGES} elementos."
    base = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()).resolve()
    attachments: list[dict] = []
    seen: set[str] = set()
    total = 0
    for item in items:
        if not isinstance(item, dict):
            return [], "Cada inline_image debe ser un objeto con path y content_id."
        content_id = str(item.get("content_id") or "").strip()
        raw_path = str(item.get("path") or "").strip()
        if not _CONTENT_ID_RE.match(content_id) or content_id in seen:
            return [], f"content_id inválido o repetido: '{content_id}'."
        if not raw_path:
            return [], f"Falta path para content_id '{content_id}'."
        candidate = Path(raw_path)
        path = (candidate if candidate.is_absolute() else base / candidate).resolve()
        if base != path and base not in path.parents:
            return [], f"La imagen '{raw_path}' está fuera del directorio de trabajo del agente."
        if not path.is_file():
            return [], f"No existe la imagen '{raw_path}'."
        size = path.stat().st_size
        if size > _MAX_INLINE_IMAGE_BYTES:
            return [], f"La imagen '{raw_path}' pesa más de {_MAX_INLINE_IMAGE_BYTES // 1000} KB."
        total += size
        if total > _MAX_INLINE_TOTAL_BYTES:
            return [], f"Las imágenes en línea suman más de {_MAX_INLINE_TOTAL_BYTES // 1000} KB."
        data = path.read_bytes()
        content_type = next((kind for magic, kind in _IMAGE_SIGNATURES if data.startswith(magic)), None)
        if not content_type:
            return [], f"'{raw_path}' no es una imagen PNG, JPEG o GIF válida."
        seen.add(content_id)
        attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": path.name,
                "contentType": content_type,
                "contentBytes": base64.b64encode(data).decode("ascii"),
                "isInline": True,
                "contentId": content_id,
            }
        )
    return attachments, None


_MAX_FILE_ATTACHMENTS = 10
_MAX_ATTACHMENTS_TOTAL_BYTES = 2_800_000
_MAX_LARGE_ATTACHMENTS_TOTAL_BYTES = 18_000_000
_SINGLE_POST_ATTACHMENT_BYTES = 2_800_000
_UPLOAD_CHUNK_BYTES = 327_680 * 10


def _load_file_attachments(
    items, used_bytes: int, max_total: int = _MAX_LARGE_ATTACHMENTS_TOTAL_BYTES
) -> tuple[list[dict], list[dict], str | None]:
    if not items:
        return [], [], None
    if isinstance(items, (str, dict)):
        items = [items]
    if not isinstance(items, list) or len(items) > _MAX_FILE_ATTACHMENTS:
        return [], [], f"attachments debe ser una lista de hasta {_MAX_FILE_ATTACHMENTS} archivos."
    base = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()).resolve()
    attachments: list[dict] = []
    summary: list[dict] = []
    total = used_bytes
    for item in items:
        raw_path = str(item.get("path") if isinstance(item, dict) else item or "").strip()
        if not raw_path:
            return [], [], "Cada adjunto necesita path (ruta del archivo en el directorio de trabajo)."
        candidate = Path(raw_path)
        path = (candidate if candidate.is_absolute() else base / candidate).resolve()
        if base != path and base not in path.parents:
            return [], [], f"El archivo '{raw_path}' está fuera del directorio de trabajo del agente."
        if not path.is_file():
            return [], [], f"No existe el archivo '{raw_path}'."
        size = path.stat().st_size
        total += size
        if total > max_total:
            return (
                [],
                [],
                f"Los adjuntos superan el máximo de {max_total / 1_000_000:.0f} MB por correo "
                f"('{raw_path}' pesa {size / 1_000_000:.1f} MB). Comparte el archivo con un enlace de "
                "SharePoint/OneDrive, divídelo o envíalo desde Outlook.",
            )
        name = str((item.get("name") if isinstance(item, dict) else "") or path.name).strip() or path.name
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        attachments.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name,
                "contentType": content_type,
                "contentBytes": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )
        summary.append({"name": name, "bytes": size, "contentType": content_type, "_path": str(path)})
    return attachments, summary, None


_DEFAULT_PREVIEW_CHARS = 200
_GET_TEXT_DEFAULT_CHARS = 8000
_GET_HTML_DEFAULT_CHARS = 30000
_GET_MIN_CHARS = 200
_GET_MAX_CHARS = 100000
_GET_FIELDS = (
    "id,subject,from,sender,toRecipients,ccRecipients,receivedDateTime,sentDateTime,"
    "isRead,hasAttachments,importance,conversationId,webLink,body"
)


class _TextExtractor(HTMLParser):
    _SKIP_TAGS = frozenset({"script", "style", "head", "title"})
    _BREAK_TAGS = frozenset(
        {"br", "p", "div", "tr", "li", "ul", "ol", "table", "blockquote", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BREAK_TAGS:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def _tidy_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return re.sub(r"\n{2,}", "\n", _tidy_text("".join(parser.parts)))


def _int_arg(value, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _cap_previews(messages: list, preview_chars: int) -> None:
    for message in messages:
        preview = message.get("bodyPreview")
        if isinstance(preview, str) and len(preview) > preview_chars:
            message["bodyPreview"] = preview[:preview_chars].rstrip() + "…"


def _parse_since(value) -> tuple[str | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, "since debe ser una fecha ISO 8601, por ejemplo 2026-10-06T20:00:00Z."
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), None


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ADDRESS_SPLIT_RE = re.compile(r"[;,\n\r]+")


def _address_tokens(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        tokens: list[str] = []
        for item in value:
            tokens.extend(_address_tokens(item))
        return tokens
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            return _address_tokens(json.loads(text))
        except ValueError:
            text = text.strip("[]")
    return [part.strip().strip("\"'") for part in _ADDRESS_SPLIT_RE.split(text) if part.strip().strip("\"'")]


def _parse_recipients(value) -> tuple[list[dict], list[str]]:
    recipients: list[dict] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for token in _address_tokens(value):
        found = _EMAIL_RE.findall(token)
        if not found:
            invalid.append(token)
            continue
        for address in found:
            key = address.lower()
            if key not in seen:
                seen.add(key)
                recipients.append({"emailAddress": {"address": address}})
    return recipients, invalid


def _recipient_list(value) -> list[dict]:
    return _parse_recipients(value)[0]


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


def _graph(method: str, path: str, access_token: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    url = _encode_url(path if path.startswith("https://") else f"{GRAPH_BASE}{path}")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    if data is None and method.upper() in ("POST", "PATCH", "PUT"):
        data = b""
    req = urllib.request.Request(
        url,
        data=data,
        method=method.upper(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            **(headers or {}),
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
            "itemNotFound": "Correo no encontrado (404).",
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


_WELL_KNOWN_FOLDERS = {
    "inbox": "inbox",
    "sentitems": "sentitems",
    "drafts": "drafts",
    "deleteditems": "deleteditems",
    "junkemail": "junkemail",
    "archive": "archive",
    "outbox": "outbox",
}
_FOLDER_ALIASES = {
    "bandeja de entrada": "inbox",
    "enviados": "sentitems",
    "elementos enviados": "sentitems",
    "borradores": "drafts",
    "eliminados": "deleteditems",
    "elementos eliminados": "deleteditems",
    "correo no deseado": "junkemail",
    "archivo": "archive",
}
_FOLDER_FIELDS = "id,displayName,childFolderCount,unreadItemCount,totalItemCount"
_MAX_FOLDERS = 300
_MAX_FOLDER_DEPTH = 4


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().strip()


def _collect_folders(token: str) -> tuple[list[dict] | None, str | None]:
    folders: list[dict] = []
    queue: list[tuple[str, str, int]] = [("/me/mailFolders", "", 1)]
    while queue and len(folders) < _MAX_FOLDERS:
        path, prefix, depth = queue.pop(0)
        result = _graph("GET", f"{path}?$top=100&$select={_FOLDER_FIELDS}", token)
        if "error" in result:
            return None, str(result["error"])
        for item in result.get("value", []):
            name = str(item.get("displayName") or "")
            full = f"{prefix}/{name}" if prefix else name
            folders.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "path": full,
                    "unread": item.get("unreadItemCount"),
                    "total": item.get("totalItemCount"),
                }
            )
            if item.get("childFolderCount") and depth < _MAX_FOLDER_DEPTH:
                queue.append((f"/me/mailFolders/{item.get('id')}/childFolders", full, depth + 1))
    return folders, None


def _resolve_folder(token: str, folder: str) -> tuple[str | None, str | None]:
    raw = str(folder or "inbox").strip() or "inbox"
    key = _fold(raw)
    if key in _WELL_KNOWN_FOLDERS:
        return _WELL_KNOWN_FOLDERS[key], None
    if key in _FOLDER_ALIASES:
        return _FOLDER_ALIASES[key], None
    if len(raw) >= 40 and " " not in raw:
        return raw, None
    folders, error = _collect_folders(token)
    if folders is None:
        return None, error
    wanted = _fold(raw.strip("/"))
    matches = [f for f in folders if _fold(f["path"]) == wanted] or [f for f in folders if _fold(f["name"]) == wanted]
    if len(matches) == 1:
        return str(matches[0]["id"]), None
    if len(matches) > 1:
        paths = ", ".join(f["path"] for f in matches[:10])
        return None, f"Hay varias carpetas llamadas '{raw}': {paths}. Indica la ruta completa."
    available = ", ".join(f["path"] for f in folders[:40])
    return None, f"No existe la carpeta '{raw}'. Carpetas disponibles: {available}."


# ── Tool handlers ─────────────────────────────────────────────────────────────


def _list_emails_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    count = min(int(args.get("count") or 10), 50)
    folder, folder_error = _resolve_folder(token, str(args.get("folder") or "inbox"))
    if folder_error:
        return json.dumps({"success": False, "error": folder_error}, ensure_ascii=False)
    since, since_error = _parse_since(args.get("since"))
    if since_error:
        return json.dumps({"success": False, "error": since_error})
    unread_only = bool(args.get("unread_only"))
    preview_chars = max(0, min(int(args.get("preview_chars") or _DEFAULT_PREVIEW_CHARS), 255))
    fields = "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview"
    filters = []
    if since or unread_only:
        filters.append(f"receivedDateTime ge {since or '1900-01-01T00:00:00Z'}")
    if unread_only:
        filters.append("isRead eq false")
    filter_clause = f"&$filter={' and '.join(filters)}" if filters else ""
    path = (
        f"/me/mailFolders/{folder}/messages?$top={count}&$select={fields}{filter_clause}"
        "&$orderby=receivedDateTime%20desc"
    )
    result = _graph("GET", path, token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    messages = result.get("value", [])
    _cap_previews(messages, preview_chars)
    return json.dumps({"success": True, "count": len(messages), "messages": messages}, ensure_ascii=False)


def _list_folders_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    folders, error = _collect_folders(token)
    if folders is None:
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    return json.dumps({"success": True, "count": len(folders), "folders": folders}, ensure_ascii=False)


def _add_candidate(found: dict, entry: dict, source: str, when: str, tokens: list[str]) -> None:
    address = str((entry or {}).get("address") or "").strip()
    name = str((entry or {}).get("name") or "").strip()
    if not address or "@" not in address:
        return
    haystack = _fold(f"{name} {address}")
    if not all(token in haystack for token in tokens):
        return
    record = found.setdefault(
        address.lower(), {"name": name, "address": address, "count": 0, "sources": set(), "last_seen": ""}
    )
    record["count"] += 1
    record["sources"].add(source)
    if name and (not record["name"] or record["name"].lower() == address.lower()):
        record["name"] = name
    if when and when > record["last_seen"]:
        record["last_seen"] = when


def _recipients_of(message: dict) -> list[dict]:
    people = []
    sender = (message.get("from") or {}).get("emailAddress")
    if sender:
        people.append(sender)
    for key in ("toRecipients", "ccRecipients"):
        people.extend((item or {}).get("emailAddress") or {} for item in message.get(key) or [])
    return people


def _find_person_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    name = " ".join(str(args.get("name") or "").replace('"', " ").split())
    if len(name) < 2:
        return json.dumps({"success": False, "error": "Se requiere name (nombre o apellido de la persona)."})
    limit = _int_arg(args.get("max_results"), 8, 1, 20)
    tokens = [t for t in _fold(name).split() if t]
    quoted = urllib.parse.quote(name, safe="")
    fields = "from,toRecipients,ccRecipients,receivedDateTime"
    found: dict = {}
    errors: list[str] = []
    for path, source in (
        (f"/me/messages?$search=%22{quoted}%22&$top=40&$select={fields}", "correo"),
        (f"/me/mailFolders/sentitems/messages?$search=%22{quoted}%22&$top=40&$select={fields}", "enviados"),
    ):
        result = _graph("GET", path, token)
        if "error" in result:
            errors.append(str(result["error"]))
            continue
        for message in result.get("value", []):
            when = str(message.get("receivedDateTime") or "")
            for person in _recipients_of(message):
                _add_candidate(found, person, source, when, tokens)
    now = datetime.now(UTC)
    window = (
        f"startDateTime={(now - timedelta(days=90)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&endDateTime={(now + timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    events = _graph("GET", f"/me/calendarView?{window}&$top=100&$select=organizer,attendees,start", token)
    if "error" not in events:
        for event in events.get("value", []):
            when = str((event.get("start") or {}).get("dateTime") or "")
            _add_candidate(found, (event.get("organizer") or {}).get("emailAddress") or {}, "reuniones", when, tokens)
            for attendee in event.get("attendees") or []:
                _add_candidate(found, (attendee or {}).get("emailAddress") or {}, "reuniones", when, tokens)
    candidates = sorted(found.values(), key=lambda c: (-c["count"], c["last_seen"]), reverse=False)
    candidates.sort(key=lambda c: (c["count"], c["last_seen"]), reverse=True)
    for candidate in candidates:
        candidate["sources"] = sorted(candidate["sources"])
    if not candidates and errors and len(errors) == 2:
        return json.dumps({"success": False, "error": errors[0]}, ensure_ascii=False)
    return json.dumps(
        {
            "success": True,
            "query": name,
            "count": len(candidates[:limit]),
            "candidates": candidates[:limit],
            "note": (
                "Candidatos tomados del historial de correo y reuniones del usuario, NO del directorio de la "
                "empresa. Confirma con el usuario cuál es antes de usarlo. Si no aparece, pídele la dirección exacta."
            ),
        },
        ensure_ascii=False,
    )


def _get_email_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    message_id = str(args.get("message_id") or "").strip()
    if not message_id:
        return json.dumps({"success": False, "error": "Se requiere message_id."})
    body_format = str(args.get("format") or "text").strip().lower()
    if body_format not in ("text", "html"):
        return json.dumps({"success": False, "error": "format debe ser text o html."})
    default_chars = _GET_HTML_DEFAULT_CHARS if body_format == "html" else _GET_TEXT_DEFAULT_CHARS
    max_chars = _int_arg(args.get("max_chars"), default_chars, _GET_MIN_CHARS, _GET_MAX_CHARS)
    headers = {"Prefer": 'outlook.body-content-type="text"'} if body_format == "text" else None
    result = _graph("GET", f"/me/messages/{message_id}?$select={_GET_FIELDS}", token, headers=headers)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    result.pop("@odata.context", None)
    body = result.get("body") or {}
    content = str(body.get("content") or "")
    if body_format == "text":
        is_html = str(body.get("contentType") or "").lower() == "html"
        content = _html_to_text(content) if is_html else _tidy_text(content)
    total_chars = len(content)
    truncated = total_chars > max_chars
    if truncated:
        content = content[:max_chars].rstrip() + "…"
    result["body"] = {"contentType": body_format, "content": content}
    result["body_chars_total"] = total_chars
    result["body_truncated"] = truncated
    return json.dumps({"success": True, "message": result}, ensure_ascii=False)


_NEEDS_MAIL_READWRITE = (
    "Para adjuntar archivos de más de 2,8 MB falta el permiso Mail.ReadWrite en la cuenta Microsoft del usuario "
    "(un administrador debe habilitarlo y el usuario reconectar su cuenta M365). Mientras tanto: comparte el "
    "archivo con un enlace de SharePoint/OneDrive, divídelo en partes menores o envíalo desde Outlook."
)


def _put_upload_chunk(url: str, chunk: bytes, start: int, total: int) -> dict:
    req = urllib.request.Request(
        url,
        data=chunk,
        method="PUT",
        headers={
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}",
            "Content-Type": "application/octet-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            return parsed if isinstance(parsed, dict) else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return {"error": f"Error {exc.code} subiendo el adjunto: {detail}"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"error": f"No se pudo subir el adjunto: {exc}"}


def _attach_file_to_draft(token: str, message_id: str, entry: dict) -> dict:
    data = Path(entry["_path"]).read_bytes()
    base = f"/me/messages/{message_id}/attachments"
    if len(data) <= _SINGLE_POST_ATTACHMENT_BYTES:
        return _graph(
            "POST",
            base,
            token,
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": entry["name"],
                "contentType": entry.get("contentType") or "application/octet-stream",
                "contentBytes": base64.b64encode(data).decode("ascii"),
            },
        )
    session = _graph(
        "POST",
        f"{base}/createUploadSession",
        token,
        {"AttachmentItem": {"attachmentType": "file", "name": entry["name"], "size": len(data)}},
    )
    if "error" in session:
        return session
    upload_url = session.get("uploadUrl")
    if not upload_url:
        return {"error": "Microsoft no entregó la dirección de subida del adjunto."}
    for start in range(0, len(data), _UPLOAD_CHUNK_BYTES):
        result = _put_upload_chunk(upload_url, data[start : start + _UPLOAD_CHUNK_BYTES], start, len(data))
        if "error" in result:
            return result
    return {}


_ONEDRIVE_FOLDER = "UltraBot-adjuntos"
_SIMPLE_UPLOAD_MAX_BYTES = 3_900_000


def _safe_drive_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .") or "archivo"
    return cleaned[:120]


def _graph_put_bytes(path: str, token: str, data: bytes) -> dict:
    url = _encode_url(f"{GRAPH_BASE}{path}")
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return {"error": f"Error {exc.code} subiendo a OneDrive: {detail}"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"error": f"No se pudo subir a OneDrive: {exc}"}


def _upload_to_onedrive(token: str, entry: dict) -> dict:
    data = Path(entry["_path"]).read_bytes()
    remote = f"/me/drive/root:/{_ONEDRIVE_FOLDER}/{_safe_drive_name(entry['name'])}"
    if len(data) <= _SIMPLE_UPLOAD_MAX_BYTES:
        item = _graph_put_bytes(f"{remote}:/content?@microsoft.graph.conflictBehavior=rename", token, data)
    else:
        session = _graph(
            "POST",
            f"{remote}:/createUploadSession",
            token,
            {"item": {"@microsoft.graph.conflictBehavior": "rename"}},
        )
        if "error" in session:
            return session
        upload_url = session.get("uploadUrl")
        if not upload_url:
            return {"error": "OneDrive no entregó la dirección de subida."}
        item = {}
        for start in range(0, len(data), _UPLOAD_CHUNK_BYTES):
            item = _put_upload_chunk(upload_url, data[start : start + _UPLOAD_CHUNK_BYTES], start, len(data))
            if "error" in item:
                return item
    if "error" in item:
        return item
    item_id = item.get("id")
    if not item_id:
        return {"error": "OneDrive no devolvió el identificador del archivo subido."}
    link = _graph("POST", f"/me/drive/items/{item_id}/createLink", token, {"type": "view", "scope": "organization"})
    if "error" in link:
        return link
    url = (link.get("link") or {}).get("webUrl")
    if not url:
        return {"error": "OneDrive no devolvió el enlace para compartir."}
    return {"url": url}


def _send_with_onedrive_links(token: str, message: dict, inline: list[dict], files: list[dict]) -> dict:
    links: list[tuple[str, str]] = []
    for entry in files:
        result = _upload_to_onedrive(token, entry)
        if "error" in result:
            return result
        links.append((entry["name"], result["url"]))
    items = "".join(f'<li><a href="{url}">{html_escape(name)}</a></li>' for name, url in links)
    footer = (
        '<p style="font-family:Arial,sans-serif;font-size:11pt">Archivos adjuntos (enlaces de OneDrive, '
        f"disponibles para quienes tengan acceso en la organización):</p><ul>{items}</ul>"
    )
    final = dict(message)
    final["body"] = {"contentType": "HTML", "content": f"{message['body']['content']}{footer}"}
    if inline:
        final["attachments"] = inline
    result = _graph("POST", "/me/sendMail", token, {"message": final})
    if "error" not in result:
        result["_links"] = [{"name": name, "url": url} for name, url in links]
    return result


def _send_via_draft(token: str, message: dict, inline: list[dict], files: list[dict]) -> dict:
    draft = _graph("POST", "/me/messages", token, message)
    if "error" in draft:
        error = str(draft["error"])
        if "403" in error or "denied" in error.lower():
            return {"error": _NEEDS_MAIL_READWRITE, "_needs_readwrite": True}
        return draft
    message_id = draft.get("id")
    if not message_id:
        return {"error": "Microsoft no devolvió el borrador del correo."}
    failure: dict | None = None
    for attachment in inline:
        result = _graph("POST", f"/me/messages/{message_id}/attachments", token, attachment)
        if "error" in result:
            failure = result
            break
    if failure is None:
        for entry in files:
            result = _attach_file_to_draft(token, message_id, entry)
            if "error" in result:
                failure = result
                break
    if failure is None:
        result = _graph("POST", f"/me/messages/{message_id}/send", token, {})
        if "error" in result:
            failure = result
    if failure is not None:
        _graph("DELETE", f"/me/messages/{message_id}", token)
        return failure
    return {}


def _send_email_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    to = args.get("to")
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "").strip()
    if not to or not subject or not body:
        return json.dumps({"success": False, "error": "Se requieren: to, subject, body."})
    recipients, invalid = _parse_recipients(to)
    if invalid:
        return json.dumps(
            {"success": False, "error": f"Destinatarios sin dirección de correo válida: {', '.join(invalid)}."},
            ensure_ascii=False,
        )
    if not recipients:
        return json.dumps({"success": False, "error": "Se requiere al menos un destinatario válido en to."})
    attachments, attachment_error = _load_inline_images(args.get("inline_images"))
    if attachment_error:
        return json.dumps({"success": False, "error": attachment_error})
    inline_bytes = sum(len(a["contentBytes"]) * 3 // 4 for a in attachments)
    files, file_summary, file_error = _load_file_attachments(args.get("attachments"), inline_bytes)
    if file_error:
        return json.dumps({"success": False, "error": file_error}, ensure_ascii=False)
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body},
        "toRecipients": recipients,
    }
    for field, graph_field in (("cc", "ccRecipients"), ("bcc", "bccRecipients")):
        extra, extra_invalid = _parse_recipients(args.get(field))
        if extra_invalid:
            return json.dumps(
                {"success": False, "error": f"Direcciones inválidas en {field}: {', '.join(extra_invalid)}."},
                ensure_ascii=False,
            )
        if extra:
            message[graph_field] = extra
    unreferenced = [a["contentId"] for a in attachments if f"cid:{a['contentId']}" not in body]
    total_bytes = inline_bytes + sum(entry["bytes"] for entry in file_summary)
    if total_bytes > _MAX_ATTACHMENTS_TOTAL_BYTES:
        result = _send_via_draft(token, message, attachments, file_summary)
        if result.get("_needs_readwrite"):
            fallback_result = _send_with_onedrive_links(token, message, attachments, file_summary)
            if "error" in fallback_result:
                result = {
                    "error": f"{_NEEDS_MAIL_READWRITE} (Además falló el envío por enlace de OneDrive: {fallback_result['error']})"
                }
            else:
                result = fallback_result
    else:
        if attachments or files:
            message["attachments"] = attachments + files
        result = _graph("POST", "/me/sendMail", token, {"message": message})
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]}, ensure_ascii=False)
    response = {"success": True, "message": "Correo enviado correctamente.", "recipients": len(recipients)}
    if file_summary:
        response["attachments"] = [{"name": e["name"], "bytes": e["bytes"]} for e in file_summary]
    if result.get("_links"):
        response["delivery"] = "enlaces_onedrive"
        response["links"] = result["_links"]
        response["warning_links"] = (
            "Los archivos grandes se enviaron como enlaces de OneDrive (acceso para la organización); "
            "destinatarios externos podrían no poder abrirlos."
        )
    if unreferenced:
        response["warning"] = f"Estas imágenes no se referencian en el HTML con cid:: {', '.join(unreferenced)}"
    return json.dumps(response)


def _search_emails_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    query = str(args.get("query") or "").replace('"', " ").strip()
    count = min(int(args.get("count") or 10), 50)
    if not query:
        return json.dumps({"success": False, "error": "Se requiere query."})
    preview_chars = _int_arg(args.get("preview_chars"), _DEFAULT_PREVIEW_CHARS, 0, 255)
    fields = "id,subject,from,receivedDateTime,isRead,bodyPreview"
    path = f"/me/messages?$search=%22{urllib.parse.quote(query, safe='')}%22&$top={count}&$select={fields}"
    result = _graph("GET", path, token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    messages = result.get("value", [])
    _cap_previews(messages, preview_chars)
    return json.dumps({"success": True, "count": len(messages), "messages": messages}, ensure_ascii=False)


# ── Plugin registration ───────────────────────────────────────────────────────


def register(ctx):
    ctx.register_tool(
        name="ms365_mail_list",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_list",
            "description": "Lista los correos del usuario desde Microsoft 365. Devuelve asunto, remitente, fecha y preview del cuerpo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Número de correos a obtener (máx 50, default 10)"},
                    "folder": {
                        "type": "string",
                        "description": (
                            "Carpeta: inbox (default), sentitems, drafts, deleteditems, o el nombre/ruta de "
                            "cualquier carpeta o subcarpeta propia (ej. 'Informaciones' o 'Bandeja de entrada/Proyectos'). "
                            "Usa ms365_mail_folders para ver cuáles existen."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": (
                            "Solo correos recibidos desde esta fecha/hora ISO 8601 (ej. 2026-10-06T20:00:00Z). "
                            "Úsalo para informes por ventana de tiempo en vez de traer 50 correos y filtrar."
                        ),
                    },
                    "unread_only": {"type": "boolean", "description": "Solo correos no leídos (default false)"},
                    "preview_chars": {
                        "type": "integer",
                        "description": "Largo máximo de la vista previa de cada correo (default 200, máx 255)",
                    },
                },
            },
        },
        handler=_list_emails_tool,
        description="Listar correos M365 del usuario",
        emoji="📬",
    )
    ctx.register_tool(
        name="ms365_mail_folders",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_folders",
            "description": (
                "Lista todas las carpetas y subcarpetas del buzón del usuario, con su ruta, "
                "cantidad de correos y no leídos. Úsalo para encontrar carpetas propias antes de ms365_mail_list."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_list_folders_tool,
        description="Listar carpetas de correo M365",
        emoji="🗂️",
    )
    ctx.register_tool(
        name="ms365_find_person",
        toolset=TOOLSET,
        schema={
            "name": "ms365_find_person",
            "description": (
                "Busca el correo de una persona por nombre o apellido en el historial de correos y reuniones "
                "del usuario (no en el directorio de la empresa). Devuelve candidatos con nombre, dirección y "
                "cuántas veces aparecen. SIEMPRE pide al usuario que confirme el candidato antes de usarlo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nombre o apellido a buscar (ej. 'Ana Torres')"},
                    "max_results": {"type": "integer", "description": "Máximo de candidatos (default 8, máx 20)"},
                },
                "required": ["name"],
            },
        },
        handler=_find_person_tool,
        description="Buscar correo de una persona por nombre",
        emoji="🔎",
    )
    ctx.register_tool(
        name="ms365_mail_get",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_get",
            "description": (
                "Obtiene un correo de Microsoft 365 por su ID. El cuerpo llega en texto plano y recortado "
                "(default 8000 caracteres); body_truncated indica si había más. Usa max_chars para ampliar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "ID del mensaje a obtener"},
                    "format": {
                        "type": "string",
                        "enum": ["text", "html"],
                        "description": "Formato del cuerpo: text (default, recomendado) o html (solo si necesitas el HTML original)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Largo máximo del cuerpo (default 8000 en text, 30000 en html; máx 100000)",
                    },
                },
                "required": ["message_id"],
            },
        },
        handler=_get_email_tool,
        description="Obtener correo M365 por ID",
        emoji="📧",
    )
    ctx.register_tool(
        name="ms365_mail_send",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_send",
            "description": (
                "Envía un correo en nombre del usuario a través de Microsoft 365, con varios destinatarios "
                "(to/cc/bcc) y archivos adjuntos (attachments). Para firmas o imágenes "
                "embebidas NO pegues base64 en el HTML: usa inline_images (archivos PNG/JPEG/GIF del "
                'directorio de trabajo) y referencia cada una en el HTML con src="cid:CONTENT_ID".'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Destinatarios: lista con una dirección por elemento (también se acepta un texto con "
                            "varias direcciones separadas por ; o ,). Admite tantos como necesites."
                        ),
                    },
                    "subject": {"type": "string", "description": "Asunto del correo"},
                    "body": {"type": "string", "description": "Cuerpo del correo (HTML o texto plano)"},
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Direcciones en copia (opcional), una por elemento",
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Direcciones en copia oculta (opcional), una por elemento",
                    },
                    "attachments": {
                        "type": "array",
                        "description": (
                            "Archivos adjuntos (PDF, Excel, Word, PowerPoint, imágenes, etc.): máx. 10 archivos y "
                            "18 MB en total. Hasta 2,8 MB se envía directo; por encima se envía por borrador y requiere "
                            "el permiso Mail.ReadWrite. Cada uno es un archivo del directorio de trabajo del agente."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Ruta del archivo dentro del directorio de trabajo del agente",
                                },
                                "name": {
                                    "type": "string",
                                    "description": "Nombre con que llega el adjunto (opcional; por defecto el del archivo)",
                                },
                            },
                            "required": ["path"],
                        },
                    },
                    "inline_images": {
                        "type": "array",
                        "description": (
                            "Imágenes incrustadas (máx. 10, 1,5 MB c/u, 2,5 MB en total). Cada una se "
                            'referencia en el HTML como <img src="cid:CONTENT_ID">.'
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Ruta de la imagen dentro del directorio de trabajo del agente",
                                },
                                "content_id": {
                                    "type": "string",
                                    "description": "Identificador para usar como cid: en el HTML (letras, números, . _ -)",
                                },
                            },
                            "required": ["path", "content_id"],
                        },
                    },
                },
                "required": ["to", "subject", "body"],
            },
        },
        handler=_send_email_tool,
        description="Enviar correo M365 en nombre del usuario",
        emoji="📤",
    )
    ctx.register_tool(
        name="ms365_mail_search",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_search",
            "description": "Busca correos en el buzón Microsoft 365 del usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Texto a buscar en asunto, remitente o cuerpo"},
                    "count": {"type": "integer", "description": "Número máximo de resultados (default 10)"},
                    "preview_chars": {
                        "type": "integer",
                        "description": "Largo máximo de la vista previa de cada correo (default 200, máx 255)",
                    },
                },
                "required": ["query"],
            },
        },
        handler=_search_emails_tool,
        description="Buscar correos en M365",
        emoji="🔍",
    )
