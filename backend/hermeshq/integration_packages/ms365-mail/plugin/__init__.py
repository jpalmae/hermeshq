from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
_MAX_LARGE_ATTACHMENTS_TOTAL_BYTES = 25_000_000
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
    _remember_ids(messages)
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


_DIRECTORY_FIELDS = "displayName,mail,userPrincipalName,jobTitle,department,officeLocation"


def _directory_search(token: str, name: str, tokens: list[str], found: dict) -> str | None:
    """Agrega al resultado las personas del directorio de la empresa; devuelve un aviso si no se pudo consultar."""
    words = name.split()
    consultas = [words]
    plegadas = [w for w in _fold(name).split() if w]
    if plegadas != [w.lower() for w in words]:
        consultas.append(plegadas)
    ultimo_error = None
    for palabras in consultas:
        if "@" in name:
            termino = f'"mail:{name}"'
        else:
            termino = " AND ".join(f'"displayName:{w}"' for w in palabras)
        ruta = f"/users?$search={urllib.parse.quote(termino, safe='')}&$top=15&$count=true&$select={_DIRECTORY_FIELDS}"
        resultado = _graph("GET", ruta, token, headers={"ConsistencyLevel": "eventual"})
        if "error" in resultado:
            ultimo_error = str(resultado["error"])
            if "403" in ultimo_error or "Authorization" in str(resultado.get("_graph_code", "")):
                return (
                    "No se pudo consultar el directorio de la empresa: la cuenta no tiene el permiso User.ReadBasic.All. "
                    "Pide al usuario que reconecte su cuenta Microsoft 365 en Mi cuenta."
                )
            continue
        agregados = 0
        for persona in resultado.get("value", []):
            direccion = str(persona.get("mail") or "").strip()
            nombre = str(persona.get("displayName") or "").strip()
            if not direccion or "@" not in direccion:
                continue
            if not all(token in _fold(f"{nombre} {direccion}") for token in tokens):
                continue
            registro = found.setdefault(
                direccion.lower(), {"name": nombre, "address": direccion, "count": 0, "sources": set(), "last_seen": ""}
            )
            registro["sources"].add("directorio")
            if nombre and (not registro["name"] or registro["name"].lower() == direccion.lower()):
                registro["name"] = nombre
            for campo, destino in (("jobTitle", "cargo"), ("department", "area"), ("officeLocation", "oficina")):
                if persona.get(campo):
                    registro[destino] = str(persona[campo])
            agregados += 1
        if agregados:
            return None
    return f"No se pudo consultar el directorio: {ultimo_error}" if ultimo_error else None


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
    directory_note = _directory_search(token, name, tokens, found)
    candidates = sorted(found.values(), key=lambda c: (-c["count"], c["last_seen"]), reverse=False)
    candidates.sort(key=lambda c: (c["count"], c["last_seen"]), reverse=True)
    for candidate in candidates:
        candidate["sources"] = sorted(candidate["sources"])
    if not candidates and errors and len(errors) == 2:
        return json.dumps({"success": False, "error": errors[0]}, ensure_ascii=False)
    resultado = {
        "success": True,
        "query": name,
        "count": len(candidates[:limit]),
        "candidates": candidates[:limit],
        "note": (
            "Candidatos del historial de correo y reuniones del usuario y del directorio de la empresa "
            "(fuente 'directorio', con cargo y área cuando existen). Confirma con el usuario cuál es antes de "
            "usarlo. Si no aparece, pídele la dirección exacta."
        ),
    }
    if directory_note:
        resultado["directorio_no_disponible"] = directory_note
    return json.dumps(resultado, ensure_ascii=False)


_ID_CACHE_MAX = 400


def _id_cache_path() -> Path:
    base = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd())
    return base / "work" / ".mail_ids.json"


def _remember_ids(messages) -> None:
    """Guarda los IDs entregados por list/search para corregir copias recortadas del modelo."""
    try:
        nuevos = [m.get("id") for m in messages if isinstance(m, dict) and m.get("id")]
        if not nuevos:
            return
        path = _id_cache_path()
        try:
            previos = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previos = []
        if not isinstance(previos, list):
            previos = []
        mezcla = [i for i in previos if i not in nuevos] + nuevos
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(mezcla[-_ID_CACHE_MAX:]), encoding="utf-8")
    except OSError:
        pass


def _resolve_message_id(given: str) -> str:
    """Si el modelo recortó o alteró el final de un ID largo, lo completa con el único ID conocido que coincide."""
    try:
        conocidos = json.loads(_id_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return given
    if not isinstance(conocidos, list) or given in conocidos:
        return given
    candidatos = [i for i in conocidos if isinstance(i, str) and i.startswith(given[:60])]
    if len(candidatos) == 1:
        return candidatos[0]
    cercanos = [i for i in candidatos if i.startswith(given.rstrip("=")[:100])]
    return cercanos[0] if len(cercanos) == 1 else given


def _fetch_message(token: str, message_id: str, body_format: str, max_chars: int) -> tuple[dict | None, str | None]:
    message_id = _resolve_message_id(message_id)
    headers = {"Prefer": 'outlook.body-content-type="text"'} if body_format == "text" else None
    result = _graph("GET", f"/me/messages/{message_id}?$select={_GET_FIELDS}", token, headers=headers)
    if "error" in result:
        return None, result["error"]
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
    return result, None


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
    message, error = _fetch_message(token, message_id, body_format, max_chars)
    if error:
        return json.dumps({"success": False, "error": error})
    return json.dumps({"success": True, "message": message}, ensure_ascii=False)


_GET_MANY_MAX = 10
_GET_MANY_DEFAULT_CHARS = 1500


def _get_emails_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    raw_ids = args.get("message_ids")
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    ids = []
    for item in raw_ids or []:
        item = str(item or "").strip()
        if item and item not in ids:
            ids.append(item)
    if not ids:
        return json.dumps({"success": False, "error": "Se requiere message_ids (lista de IDs)."})
    omitted = ids[_GET_MANY_MAX:]
    ids = ids[:_GET_MANY_MAX]
    max_chars = _int_arg(args.get("max_chars"), _GET_MANY_DEFAULT_CHARS, _GET_MIN_CHARS, _GET_MAX_CHARS)

    def one(message_id):
        message, error = _fetch_message(token, message_id, "text", max_chars)
        return {"message_id": message_id, "error": error} if error else message

    with ThreadPoolExecutor(max_workers=5) as pool:
        messages = list(pool.map(one, ids))
    payload = {"success": True, "count": len(messages), "messages": messages}
    if omitted:
        payload["omitted_ids"] = omitted
        payload["note"] = f"Máximo {_GET_MANY_MAX} correos por llamada; pide el resto en otra llamada."
    return json.dumps(payload, ensure_ascii=False)


_APPROVAL_TTL_SECONDS = 1800


_MAX_DOWNLOAD_ATTACHMENTS = 10


_MAX_DOWNLOAD_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _current_task_id() -> str:
    raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    try:
        return str(json.loads(raw).get("task_id") or "") if raw else ""
    except (ValueError, AttributeError):
        return ""


def _previews_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".mail_previews.json"


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


def _action_fingerprint(user_id: str, *parts) -> str:
    return hashlib.sha256(
        "|".join([user_id, *[json.dumps(p, sort_keys=True, default=str) for p in parts]]).encode()
    ).hexdigest()


def _approval_gate(args: dict, fingerprint: str, preview: dict) -> str | None:
    """Devuelve la respuesta de vista previa si todavía no hay una aprobación real (el usuario respondió tras verla)."""
    previews = _load_previews()
    entry = previews.get(fingerprint)
    current = _current_task_id()
    real = bool(
        args.get("aprobado_por_el_usuario") and entry and current and entry.get("task") and entry["task"] != current
    )
    if real:
        return None
    previews[fingerprint] = {"task": current, "ts": time.time()}
    _save_previews(previews)
    result = {
        "success": False,
        "requiere_aprobacion": True,
        "vista_previa": preview,
        "siguiente": "Muéstrale al usuario esta vista previa y pregunta si lo envías; termina tu respuesta ahí y espera "
        "su siguiente mensaje. Cuando diga que sí, vuelve a llamar con aprobado_por_el_usuario=true y EXACTAMENTE "
        "los mismos datos.",
    }
    if args.get("aprobado_por_el_usuario"):
        result["error"] = (
            "No se envió: esta vista previa aún no se le mostró al usuario en un mensaje anterior. No puedes aprobar "
            "por él en la misma respuesta."
        )
    return json.dumps(result, ensure_ascii=False)


def _consume_approval(fingerprint: str) -> None:
    previews = _load_previews()
    previews.pop(fingerprint, None)
    _save_previews(previews)


def _list_attachments(token: str, message_id: str) -> tuple[list[dict] | None, str | None]:
    result = _graph(
        "GET", f"/me/messages/{message_id}/attachments?$select=id,name,size,isInline,contentType&$top=50", token
    )
    if "error" in result:
        return None, str(result["error"])
    return result.get("value", []), None


def _attachment_summary(item: dict) -> dict:
    return {
        "nombre": item.get("name"),
        "tamano_kb": round(int(item.get("size") or 0) / 1024),
        "incrustado": bool(item.get("isInline")),
        "tipo": item.get("contentType"),
    }


def _message_for_action(token: str, message_id: str) -> tuple[dict | None, str | None]:
    message = _graph(
        "GET",
        f"/me/messages/{message_id}?$select=subject,from,toRecipients,receivedDateTime,hasAttachments",
        token,
    )
    if "error" in message:
        return None, str(message["error"])
    return message, None


def _forward_email_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    message_id = _resolve_message_id(str(args.get("message_id") or "").strip())
    to, invalid_to = _parse_recipients(args.get("to"))
    cc, invalid_cc = _parse_recipients(args.get("cc"))
    comment = str(args.get("comment") or "").strip()
    if not message_id or not to or invalid_to or invalid_cc:
        return json.dumps(
            {
                "success": False,
                "error": "Se requiere message_id y to (correos válidos). "
                + (f"Direcciones inválidas: {invalid_to + invalid_cc}. " if invalid_to or invalid_cc else "")
                + "Si solo tienes el nombre, búscalo con ms365_find_person y confírmalo con el usuario.",
            },
            ensure_ascii=False,
        )
    message, error = _message_for_action(token, message_id)
    if message is None:
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    attachments, _ = _list_attachments(token, message_id)
    preview = {
        "accion": "reenviar",
        "asunto": message.get("subject"),
        "de": ((message.get("from") or {}).get("emailAddress") or {}).get("address"),
        "fecha": message.get("receivedDateTime"),
        "para": [r["emailAddress"]["address"] for r in to],
        "cc": [r["emailAddress"]["address"] for r in cc],
        "comentario": comment or None,
        "adjuntos_e_imagenes_que_viajan": [_attachment_summary(a) for a in attachments or []],
        "nota": "Se reenvía el correo ORIGINAL completo: conserva sus adjuntos e imágenes incrustadas.",
    }
    fingerprint = _action_fingerprint(
        user_id, "forward", message_id, [r["emailAddress"]["address"] for r in to + cc], comment
    )
    gate = _approval_gate(args, fingerprint, preview)
    if gate is not None:
        return gate
    if cc:
        draft = _graph(
            "POST", f"/me/messages/{message_id}/createForward", token, {"toRecipients": to, "comment": comment}
        )
        if "error" in draft:
            detail = str(draft["error"])
            if "403" in detail:
                detail += " Para reenviar con copia falta el permiso Mail.ReadWrite: el usuario debe reconectar su cuenta Microsoft 365."
            return json.dumps({"success": False, "error": detail}, ensure_ascii=False)
        patched = _graph("PATCH", f"/me/messages/{draft.get('id')}", token, {"ccRecipients": cc})
        sent = _graph("POST", f"/me/messages/{draft.get('id')}/send", token) if "error" not in patched else patched
    else:
        sent = _graph("POST", f"/me/messages/{message_id}/forward", token, {"toRecipients": to, "comment": comment})
    if "error" in sent:
        return json.dumps({"success": False, "error": sent["error"]}, ensure_ascii=False)
    _consume_approval(fingerprint)
    return json.dumps(
        {"success": True, "reenviado_a": preview["para"], "cc": preview["cc"], "asunto": preview["asunto"]},
        ensure_ascii=False,
    )


def _reply_email_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    message_id = _resolve_message_id(str(args.get("message_id") or "").strip())
    comment = str(args.get("comment") or "").strip()
    reply_all = bool(args.get("reply_all"))
    if not message_id or not comment:
        return json.dumps({"success": False, "error": "Se requiere message_id y comment (el texto de la respuesta)."})
    message, error = _message_for_action(token, message_id)
    if message is None:
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    preview = {
        "accion": "responder a todos" if reply_all else "responder",
        "asunto": message.get("subject"),
        "responde_a": ((message.get("from") or {}).get("emailAddress") or {}).get("address"),
        "tambien_a": [((r or {}).get("emailAddress") or {}).get("address") for r in message.get("toRecipients") or []]
        if reply_all
        else [],
        "texto": comment,
    }
    fingerprint = _action_fingerprint(user_id, "reply", message_id, reply_all, comment)
    gate = _approval_gate(args, fingerprint, preview)
    if gate is not None:
        return gate
    sent = _graph(
        "POST", f"/me/messages/{message_id}/{'replyAll' if reply_all else 'reply'}", token, {"comment": comment}
    )
    if "error" in sent:
        return json.dumps({"success": False, "error": sent["error"]}, ensure_ascii=False)
    _consume_approval(fingerprint)
    return json.dumps(
        {"success": True, "respondido": preview["accion"], "asunto": preview["asunto"]}, ensure_ascii=False
    )


def _graph_get_bytes(path: str, token: str) -> tuple[bytes | None, str]:
    req = urllib.request.Request(
        _encode_url(f"{GRAPH_BASE}{path}"), headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(_MAX_DOWNLOAD_ATTACHMENT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return None, f"Error {exc.code} al descargar el adjunto."
    except (urllib.error.URLError, TimeoutError) as exc:
        return None, f"No se pudo descargar el adjunto: {exc}"
    if len(data) > _MAX_DOWNLOAD_ATTACHMENT_BYTES:
        return None, f"Supera el máximo de {_MAX_DOWNLOAD_ATTACHMENT_BYTES // (1024 * 1024)} MB."
    return data, ""


def _mail_attachments_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    message_id = _resolve_message_id(str(args.get("message_id") or "").strip())
    if not message_id:
        return json.dumps({"success": False, "error": "Se requiere message_id."})
    items, error = _list_attachments(token, message_id)
    if items is None:
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    result: dict = {"success": True, "count": len(items), "adjuntos": [_attachment_summary(i) for i in items]}
    if not args.get("download"):
        result["siguiente"] = (
            "Con download=true los guardo en el workspace (incluye imágenes incrustadas con include_inline=true)."
        )
        return json.dumps(result, ensure_ascii=False)
    folder = (
        Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd())
        / "mail_attachments"
        / re.sub(r"\W", "", message_id)[-12:]
    )
    folder.mkdir(parents=True, exist_ok=True)
    saved, skipped = [], []
    for item in items:
        name = str(item.get("name") or "adjunto")
        if item.get("isInline") and not args.get("include_inline"):
            skipped.append({"nombre": name, "motivo": "imagen incrustada (usa include_inline=true)"})
            continue
        if len(saved) >= _MAX_DOWNLOAD_ATTACHMENTS:
            skipped.append({"nombre": name, "motivo": f"máximo {_MAX_DOWNLOAD_ATTACHMENTS} archivos"})
            continue
        data, derr = _graph_get_bytes(f"/me/messages/{message_id}/attachments/{item.get('id')}/$value", token)
        if data is None:
            skipped.append({"nombre": name, "motivo": derr})
            continue
        safe = re.sub(r"[^\w.\- ]", "_", name).strip(" .") or "adjunto"
        target = folder / safe
        target.write_bytes(data)
        saved.append(
            {
                "nombre": name,
                "tamano_kb": round(len(data) / 1024),
                "local_path": str(target.relative_to(folder.parents[1])),
            }
        )
    result.update({"descargados": saved, "omitidos": skipped})
    result["hint"] = "Archivos en el workspace; para reenviar el correo con todo su contenido usa ms365_mail_forward."
    return json.dumps(result, ensure_ascii=False)


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


def _kql_value(value) -> str:
    return str(value or "").replace('"', " ").strip()


def _kql_date(value, label) -> tuple[str | None, str | None]:
    parsed, error = _parse_since(value)
    if error:
        return None, error.replace("since", label)
    return (parsed[:10] if parsed else None), None


def _build_search_kql(args: dict) -> tuple[str | None, str | None]:
    parts = []
    query = _kql_value(args.get("query"))
    if query:
        parts.append(query)
    sender = _kql_value(args.get("from"))
    if sender:
        parts.append(f'from:"{sender}"')
    subject = _kql_value(args.get("subject"))
    if subject:
        parts.append(f'subject:"{subject}"')
    since, error = _kql_date(args.get("since"), "since")
    if error:
        return None, error
    if since:
        parts.append(f"received>={since}")
    until, error = _kql_date(args.get("until"), "until")
    if error:
        return None, error
    if until:
        parts.append(f"received<={until}")
    if args.get("has_attachments"):
        parts.append("hasattachments:true")
    if not parts:
        return None, "Indica al menos query, from, subject, since, until o has_attachments."
    return " ".join(parts), None


def _search_emails_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    kql, kql_error = _build_search_kql(args)
    if kql_error:
        return json.dumps({"success": False, "error": kql_error}, ensure_ascii=False)
    count = min(int(args.get("count") or 10), 50)
    preview_chars = _int_arg(args.get("preview_chars"), _DEFAULT_PREVIEW_CHARS, 0, 255)
    base = "/me/messages"
    if str(args.get("folder") or "").strip():
        folder, folder_error = _resolve_folder(token, str(args["folder"]))
        if folder_error:
            return json.dumps({"success": False, "error": folder_error}, ensure_ascii=False)
        base = f"/me/mailFolders/{folder}/messages"
    fields = "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview"
    path = f"{base}?$search=%22{urllib.parse.quote(kql, safe='')}%22&$top={count}&$select={fields}"
    result = _graph("GET", path, token)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    messages = sorted(result.get("value", []), key=lambda m: m.get("receivedDateTime") or "", reverse=True)
    _cap_previews(messages, preview_chars)
    _remember_ids(messages)
    return json.dumps(
        {"success": True, "count": len(messages), "search": kql, "messages": messages}, ensure_ascii=False
    )


# ── Plugin registration ───────────────────────────────────────────────────────


_ESTADO_MAX_KB = 25
_ESTADO_ESCALA_HORAS = 2
_ESTADO_TIBIO_HORAS = 24
_ESTADO_BACKLOG_HORAS = 72
_ESTADO_TOPE_CRITICOS = 10
_NIVELES_ESTADO = ("CALIENTE", "TIBIO", "FRIO")


def _estado_path() -> Path:
    base = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd())
    return base / "work" / "cron_inbox_estado.json"


def _estado_nivel(valor) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or "")).encode("ascii", "ignore").decode().upper().strip()
    return "CALIENTE" if texto == "CRITICO" else texto if texto in _NIVELES_ESTADO else "TIBIO"


def _estado_fecha(valor, por_defecto):
    texto = str(valor or "").strip()
    if not texto:
        return por_defecto
    try:
        fecha = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return por_defecto
    return (fecha if fecha.tzinfo else fecha.replace(tzinfo=UTC)).astimezone(UTC)


def _estado_item(item, ahora, reportado=True):
    if not isinstance(item, dict):
        return None
    identificador = str(item.get("id") or item.get("key") or "").strip()
    asunto = str(item.get("subject") or item.get("asunto") or "").strip()
    if not identificador and not asunto:
        return None
    primera = _estado_fecha(item.get("first_seen") or item.get("recibido"), ahora)
    return {
        "id": identificador or asunto[:40],
        "subject": asunto[:90],
        "from": str(item.get("from") or item.get("sender") or item.get("remitente") or "")[:40],
        "first_seen": primera.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": _estado_nivel(item.get("level") or item.get("nivel")),
        "reported": bool(item.get("reported", reportado)),
    }


def _estado_cargar(ahora):
    path = _estado_path()
    datos = {}
    if path.is_file():
        try:
            datos = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            datos = {}
    pendientes = [i for i in (_estado_item(x, ahora) for x in datos.get("pending") or []) if i]
    return datos, pendientes


def _estado_horas(item, ahora) -> float:
    return max(0.0, (ahora - _estado_fecha(item["first_seen"], ahora)).total_seconds() / 3600)


def _estado_vista(item, ahora):
    horas = _estado_horas(item, ahora)
    nivel = "CRITICO" if item["level"] == "CALIENTE" and horas >= _ESTADO_ESCALA_HORAS else item["level"]
    return {
        "asunto": item["subject"],
        "de": item["from"],
        "nivel": nivel,
        "horas": round(horas, 1),
        "desde": item["first_seen"][:16],
    }


def _estado_resumen_backlog(items, previo):
    remitentes = {}
    for item in items:
        remitentes[item["from"] or "?"] = remitentes.get(item["from"] or "?", 0) + 1
    antiguos = {"count": int(previo.get("count") or 0) + len(items), "remitentes": {}, "desde": None, "hasta": None}
    for nombre, cantidad in (previo.get("remitentes") or {}).items():
        remitentes[nombre] = remitentes.get(nombre, 0) + cantidad
    antiguos["remitentes"] = dict(sorted(remitentes.items(), key=lambda par: -par[1])[:8])
    fechas = [i["first_seen"][:10] for i in items] + [f for f in (previo.get("desde"), previo.get("hasta")) if f]
    if fechas:
        antiguos["desde"], antiguos["hasta"] = min(fechas), max(fechas)
    return antiguos


def _estado_no_leidos():
    """Ids de los correos sin leer del inbox (hasta 100), o None si no se pudo consultar o hay más de 100."""
    user_id = _task_user_id()
    if not user_id:
        return None
    token, _ = _get_m365_token(user_id)
    if not token:
        return None
    resultado = _graph(
        "GET",
        "/me/mailFolders/inbox/messages?$top=100&$select=id&$filter=isRead%20eq%20false",
        token,
    )
    if "error" in resultado:
        return None
    ids = [m.get("id") for m in resultado.get("value", []) if isinstance(m, dict) and m.get("id")]
    return None if len(ids) >= 100 else set(ids)


def _estado_ventana(ahora):
    """Desde cuándo leer el inbox: lun-vie la hora anterior; sáb-dom desde el último run (o el inicio del día)."""
    from zoneinfo import ZoneInfo

    local = ahora.astimezone(ZoneInfo("America/Santiago"))
    datos, pendientes = _estado_cargar(ahora)
    if local.weekday() >= 5:
        previo = _estado_fecha(datos.get("last_run"), None)
        inicio_dia = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        desde = previo if previo and previo > inicio_dia else inicio_dia
        modo, cantidad = "fin de semana: desde el último informe", 20
    else:
        desde = ahora.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        modo, cantidad = "lunes a viernes: la última hora", 15
    return {
        "success": True,
        "since": desde.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": cantidad,
        "modo": modo,
        "hora_chile": local.strftime("%A %Y-%m-%d %H:%M"),
        "pendientes_guardados": len(pendientes),
        "siguiente": "Llama ms365_mail_list(folder=inbox, since=<since>, count=<count>, preview_chars=160), clasifica los nuevos y luego ms365_inbox_estado(nuevos=[...]).",
    }


def _inbox_estado_tool(args, **_kwargs):
    if args.get("accion") == "ventana":
        try:
            return json.dumps(_estado_ventana(_estado_fecha(args.get("ahora"), datetime.now(UTC))), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001  # report instead of crashing the run
            return json.dumps({"success": False, "error": f"No se pudo calcular la ventana: {exc}"}, ensure_ascii=False)
    try:
        ahora = _estado_fecha(args.get("ahora"), datetime.now(UTC))
        datos, pendientes = _estado_cargar(ahora)
        previo = datos.get("backlog_antiguo") if isinstance(datos.get("backlog_antiguo"), dict) else {}
        resueltos = {_resolve_message_id(str(x).strip()) for x in args.get("resueltos") or [] if str(x).strip()}
        no_leidos = _estado_no_leidos() if args.get("verificar_leidos", True) else None
        if no_leidos is not None:
            resueltos |= {p["id"] for p in pendientes if len(p["id"]) >= 60 and p["id"] not in no_leidos}
        existentes = {p["id"]: p for p in pendientes}
        nuevos_vistos = []
        for crudo in args.get("nuevos") or []:
            item = _estado_item(crudo, ahora, reportado=False)
            if item is None:
                continue
            item["id"] = _resolve_message_id(item["id"])
            if item["id"] in existentes:
                if item["level"] == "CALIENTE":
                    existentes[item["id"]]["level"] = "CALIENTE"
                continue
            existentes[item["id"]] = item
            nuevos_vistos.append(item["id"])
        pendientes = [p for p in existentes.values() if p["id"] not in resueltos]
        quedan, al_backlog, descartados = [], [], 0
        for item in pendientes:
            horas = _estado_horas(item, ahora)
            if item["level"] == "FRIO" and item["reported"] and item["id"] not in nuevos_vistos:
                descartados += 1
            elif horas > _ESTADO_BACKLOG_HORAS or (item["level"] == "TIBIO" and horas > _ESTADO_TIBIO_HORAS):
                al_backlog.append(item)
            else:
                quedan.append(item)
        backlog = _estado_resumen_backlog(al_backlog, previo)
        criticos = sorted(
            (p for p in quedan if _estado_vista(p, ahora)["nivel"] == "CRITICO"),
            key=lambda p: p["first_seen"],
            reverse=True,
        )
        mostrar_criticos = criticos[:_ESTADO_TOPE_CRITICOS]
        antiguos = [p for p in criticos[_ESTADO_TOPE_CRITICOS:]]
        resto = [p for p in quedan if p not in criticos]
        sin_cambios = [p for p in resto if p["reported"] and p["id"] not in nuevos_vistos and p["level"] != "CALIENTE"]
        salida = {
            "success": True,
            "totales": {
                "pendientes": len(quedan),
                "criticos": len(criticos),
                "calientes": sum(1 for p in resto if p["level"] == "CALIENTE"),
                "tibios": sum(1 for p in resto if p["level"] == "TIBIO"),
                "frios": sum(1 for p in resto if p["level"] == "FRIO"),
            },
            "nuevos_este_run": [_estado_vista(p, ahora) for p in quedan if p["id"] in nuevos_vistos],
            "criticos": [_estado_vista(p, ahora) for p in mostrar_criticos],
            "criticos_mas_antiguos": {
                "cantidad": len(antiguos),
                "remitentes": dict(
                    sorted(_estado_resumen_backlog(antiguos, {})["remitentes"].items(), key=lambda par: -par[1])[:5]
                ),
            },
            "calientes": [_estado_vista(p, ahora) for p in resto if p["level"] == "CALIENTE"][:15],
            "pendientes_sin_cambios": len(sin_cambios),
            "descartados_frio_ya_reportados": descartados,
            "backlog_antiguo": backlog,
        }
        if args.get("primer_informe"):
            salida["detalle_criticos_antiguos"] = [_estado_vista(p, ahora) for p in antiguos[:30]]
        for item in quedan:
            if item["id"] in nuevos_vistos:
                item["reported"] = True
        escrito = {
            "last_run": ahora.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timezone": datos.get("timezone") or "America/Santiago",
            "pending": quedan,
            "backlog_antiguo": backlog,
            "dismissed": (datos.get("dismissed") or [])[-20:],
        }
        texto = json.dumps(escrito, ensure_ascii=False, separators=(",", ":"))
        path = _estado_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporal = path.with_suffix(".tmp")
        temporal.write_text(texto, encoding="utf-8")
        temporal.replace(path)
        salida["archivo_kb"] = round(len(texto.encode("utf-8")) / 1024, 1)
        if salida["archivo_kb"] > _ESTADO_MAX_KB:
            salida["aviso"] = f"El estado pesa más de {_ESTADO_MAX_KB} KB; revisa los pendientes."
        salida["siguiente"] = (
            "Arma el informe con estos datos (no abras ni modifiques work/cron_inbox_estado.json, ya quedó actualizado)."
        )
        return json.dumps(salida, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001  # state errors must be reported, not crash the run
        return json.dumps({"success": False, "error": f"No se pudo actualizar el estado: {exc}"}, ensure_ascii=False)


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
                "Busca el correo de una persona por nombre o apellido en el historial de correos y reuniones del "
                "usuario Y en el directorio de toda la empresa. Devuelve candidatos con nombre, dirección, cargo y área "
                "(si es del directorio), cuántas veces aparecen en el historial y la fuente. SIEMPRE pide al usuario que "
                "confirme el candidato antes de usarlo."
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
        name="ms365_mail_get_many",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_get_many",
            "description": (
                "Obtiene VARIOS correos de Microsoft 365 por sus IDs en una sola llamada (hasta 10, en paralelo). "
                "Úsala en vez de llamar ms365_mail_get una vez por correo: es mucho más rápido. "
                "Cada cuerpo llega en texto plano recortado a max_chars (default 1500)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IDs de los mensajes (máximo 10)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Largo máximo del cuerpo de cada correo (default 1500, máx 100000)",
                    },
                },
                "required": ["message_ids"],
            },
        },
        handler=_get_emails_tool,
        description="Obtener varios correos M365 por ID",
        emoji="📚",
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
        name="ms365_mail_forward",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_forward",
            "description": (
                "REENVÍA un correo recibido a otras personas conservando TODO su contenido: cuerpo, adjuntos e imágenes "
                "incrustadas (capturas, logos). Úsala siempre que pidan «reenvía el correo de X a Y»; NO intentes sacar "
                "los adjuntos ni armar un correo nuevo con ms365_mail_send. La primera llamada devuelve una vista previa "
                "(asunto, de quién es, adjuntos que viajan, destinatarios) sin enviar: muéstrasela al usuario y espera "
                "su respuesta; solo con aprobado_por_el_usuario=true y los mismos datos, en un mensaje posterior, se envía."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "ID del correo (de ms365_mail_list o ms365_mail_search).",
                    },
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Destinatarios (correos exactos).",
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Copia (opcional; requiere Mail.ReadWrite).",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Texto que se agrega arriba del correo reenviado (opcional).",
                    },
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true SOLO si el usuario ya vio la vista previa y dijo que sí.",
                    },
                },
                "required": ["message_id", "to"],
            },
        },
        handler=_forward_email_tool,
        description="Reenviar un correo recibido con sus adjuntos (con aprobación)",
        emoji="↪️",
    )
    ctx.register_tool(
        name="ms365_mail_reply",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_reply",
            "description": (
                "RESPONDE un correo recibido (al remitente o a todos) dentro del mismo hilo, con el texto citado debajo. "
                "La primera llamada devuelve una vista previa sin enviar; solo con aprobado_por_el_usuario=true y los "
                "mismos datos, en un mensaje posterior del usuario, se envía."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "ID del correo a responder."},
                    "comment": {"type": "string", "description": "Texto de la respuesta."},
                    "reply_all": {"type": "boolean", "description": "true: responder a todos."},
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true SOLO si el usuario ya vio la vista previa y dijo que sí.",
                    },
                },
                "required": ["message_id", "comment"],
            },
        },
        handler=_reply_email_tool,
        description="Responder un correo recibido (con aprobación)",
        emoji="↩️",
    )
    ctx.register_tool(
        name="ms365_mail_attachments",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_attachments",
            "description": (
                "Lista los adjuntos y las imágenes incrustadas de un correo RECIBIDO (nombre, peso, tipo) y, con "
                "download=true, los guarda en el workspace (hasta 10 archivos de 10 MB) para leerlos, analizarlos con "
                "vision_analyze o adjuntarlos a otro correo. Si lo que quieren es reenviar el correo, usa mejor "
                "ms365_mail_forward."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "ID del correo."},
                    "download": {"type": "boolean", "description": "true: descargar al workspace."},
                    "include_inline": {
                        "type": "boolean",
                        "description": "true: incluir también las imágenes incrustadas.",
                    },
                },
                "required": ["message_id"],
            },
        },
        handler=_mail_attachments_tool,
        description="Ver y descargar los adjuntos de un correo recibido",
        emoji="📎",
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
                            "25 MB en total. Hasta 2,8 MB se envía directo; por encima se envía por borrador y requiere "
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
        name="ms365_inbox_estado",
        toolset=TOOLSET,
        schema={
            "name": "ms365_inbox_estado",
            "description": "Mantiene el estado del informe horario de correos (work/cron_inbox_estado.json) en UNA llamada y entrega los datos para el informe: agrega los correos nuevos ya clasificados, quita los resueltos, escala a CRITICO solo los CALIENTE con 2 h o más, aplica la higiene (FRIO ya reportados, TIBIO de más de 24 h y todo de más de 72 h pasan a un resumen) y devuelve los 10 críticos más recientes, nuevos, pendientes sin cambios y backlog resumido. NO escribas scripts ni edites ese archivo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nuevos": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Correos nuevos de este run: [{id, asunto, remitente, recibido (ISO UTC), nivel: CALIENTE|TIBIO|FRIO}].",
                    },
                    "resueltos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ids de correos ya leídos o respondidos que deben salir de pendientes.",
                    },
                    "accion": {
                        "type": "string",
                        "description": "'ventana' (sin más parámetros) devuelve desde cuándo leer el inbox (since y count) según sea lunes a viernes o fin de semana. Úsala primero.",
                    },
                    "verificar_leidos": {
                        "type": "boolean",
                        "description": "Por defecto true: consulta el inbox y quita de pendientes lo que ya se leyó. No hace falta pasar 'resueltos'.",
                    },
                    "primer_informe": {
                        "type": "boolean",
                        "description": "true en el primer informe del día (08:00): incluye el detalle de los críticos antiguos.",
                    },
                },
                "required": [],
            },
        },
        handler=_inbox_estado_tool,
        description="Actualizar el estado del informe horario de correos",
        emoji="📬",
    )
    ctx.register_tool(
        name="ms365_mail_search",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_search",
            "description": "Busca correos en el buzón Microsoft 365 del usuario (todo el buzón o una carpeta). Combina palabras con filtros from, subject, since, until, has_attachments y folder en UNA sola llamada: no pruebes variantes de palabras una por una. Devuelve los resultados ordenados del más reciente al más antiguo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Palabras a buscar en asunto, remitente o cuerpo (todas deben aparecer)",
                    },
                    "from": {"type": "string", "description": "Filtra por remitente (nombre o correo)"},
                    "subject": {"type": "string", "description": "Filtra por texto del asunto"},
                    "since": {
                        "type": "string",
                        "description": "Solo correos desde esta fecha (ISO 8601, p. ej. 2026-10-07)",
                    },
                    "until": {"type": "string", "description": "Solo correos hasta esta fecha (ISO 8601)"},
                    "has_attachments": {"type": "boolean", "description": "Solo correos con adjuntos"},
                    "folder": {
                        "type": "string",
                        "description": "Carpeta donde buscar (inbox, sentitems o ruta); por defecto todo el buzón",
                    },
                    "count": {"type": "integer", "description": "Número máximo de resultados (default 10, máx 50)"},
                    "preview_chars": {
                        "type": "integer",
                        "description": "Largo máximo de la vista previa de cada correo (default 200, máx 255)",
                    },
                },
                "required": [],
            },
        },
        handler=_search_emails_tool,
        description="Buscar correos en M365",
        emoji="🔍",
    )
