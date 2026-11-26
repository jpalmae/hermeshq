from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
_CU_BLOQUEO_ESPERA_S = 30
_bloqueos_del_hilo = threading.local()


class EstadoOcupado(RuntimeError):
    pass


@contextmanager
def _bloqueo_archivo(path: Path):
    tenidos = getattr(_bloqueos_del_hilo, "tenidos", None)
    if tenidos is None:
        tenidos = _bloqueos_del_hilo.tenidos = {}
    clave = str(path)
    if tenidos.get(clave):
        tenidos[clave] += 1
        try:
            yield
        finally:
            tenidos[clave] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.parent / f"{path.name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        limite = time.monotonic() + float(_CU_BLOQUEO_ESPERA_S)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= limite:
                    raise EstadoOcupado(
                        f"estado {path.name} ocupado por otra ejecución (cron o chat) por más de "
                        f"{_CU_BLOQUEO_ESPERA_S} s; no se escribió nada, reintenta en un momento"
                    ) from None
                time.sleep(0.05)
        tenidos[clave] = 1
        try:
            yield
        finally:
            tenidos.pop(clave, None)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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


_DIAS_SEMANA = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_ZONA_USUARIO = "America/Santiago"


def _hora_chile(valor) -> str | None:
    texto = str(valor or "").strip()
    if not texto:
        return None
    try:
        from zoneinfo import ZoneInfo

        instante = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        if instante.tzinfo is None:
            instante = instante.replace(tzinfo=UTC)
        local = instante.astimezone(ZoneInfo(_ZONA_USUARIO))
    except (ValueError, KeyError):
        return None
    return f"{_DIAS_SEMANA[local.weekday()]} {local:%d-%m-%Y %H:%M}"


def _con_hora_local(messages: list) -> None:
    for message in messages:
        if not isinstance(message, dict):
            continue
        for origen, destino in (("receivedDateTime", "recibido_chile"), ("sentDateTime", "enviado_chile")):
            local = _hora_chile(message.get(origen))
            if local:
                message[destino] = local


def _ahora_chile() -> str:
    return _hora_chile(datetime.now(UTC).isoformat()) or ""


_AVISO_IDS_CORTOS = (
    "Solo los correos con id_corto tienen #ID. Un correo sin id_corto NO tiene #ID: no le asignes ni le inventes uno, "
    "ni reutilices el #ID de otro correo; identifícalo por asunto, remitente y fecha."
)


def _mapa_ids_cortos() -> tuple[dict, dict]:
    try:
        datos = _cu_load()
    except Exception:  # noqa: BLE001
        return {}, {}
    por_mensaje, por_id = {}, {}
    for sec in ("pending", "resolved", "discarded"):
        for it in datos.get(sec) or []:
            if not isinstance(it, dict) or not it.get("display_id"):
                continue
            did = _cu_norm_display(it.get("display_id"))
            por_id[did] = str(it.get("subject") or "")
            if it.get("message_id"):
                por_mensaje[str(it["message_id"])] = did
            imid = str(it.get("internet_message_id") or "").strip().lower()
            if imid:
                por_mensaje["imid:" + imid] = did
    return por_mensaje, por_id


def mapa_ids_correo() -> dict:
    return _mapa_ids_cortos()[1]


def _anotar_ids_cortos(messages: list) -> bool:
    por_mensaje, _ = _mapa_ids_cortos()
    sin_id = False
    for m in messages:
        if not isinstance(m, dict) or not (m.get("id") or m.get("message_id")):
            continue
        imid = str(m.get("internetMessageId") or m.get("internet_message_id") or "").strip().lower()
        did = por_mensaje.get(str(m.get("id") or m.get("message_id") or "")) or (
            por_mensaje.get("imid:" + imid) if imid else None
        )
        if did:
            m["id_corto"] = did
        else:
            sin_id = True
    return sin_id


def _cap_previews(messages: list, preview_chars: int) -> None:
    for message in messages:
        preview = message.get("bodyPreview")
        if isinstance(preview, str) and len(preview) > preview_chars:
            message["bodyPreview"] = preview[:preview_chars].rstrip() + "…"


def _parse_since(value, hora_de_chile: bool = False) -> tuple[str | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, "since debe ser una fecha ISO 8601, por ejemplo 2026-10-06T20:00:00Z."
    if parsed.tzinfo is None:
        if hora_de_chile:
            from zoneinfo import ZoneInfo

            parsed = parsed.replace(tzinfo=ZoneInfo(_ZONA_USUARIO))
        else:
            parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), None


def _desde_chile(since: str) -> str:
    from zoneinfo import ZoneInfo

    local = datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone(ZoneInfo(_ZONA_USUARIO))
    return local.strftime("%d-%m-%Y %H:%M") + " (hora de Chile)"


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


_YO = re.compile(
    r"^(yo|me|m[ií]|a m[ií](\s+mism[oa])?|m[ií]\s+mism[oa]|mi (propio )?(correo|buz[oó]n)|mi propia cuenta)$", re.I
)
_DOMINIOS_DE_EJEMPLO = re.compile(
    r"@(example|ejemplo|dominio|tudominio|midominio|empresa|correo|email|domain|test|usuario|"
    r"cuentamicrosoft365|microsoft365)\.(com|org|net|cl)$",
    re.I,
)


def _reemplazar_yo(value, token: str):
    tokens = _address_tokens(value)
    if not any(_YO.match(t) for t in tokens):
        return value
    yo = _graph("GET", "/me?$select=mail,userPrincipalName", token)
    propia = (
        "" if not isinstance(yo, dict) or "error" in yo else str(yo.get("mail") or yo.get("userPrincipalName") or "")
    )
    if not propia:
        return value
    return [propia if _YO.match(t) else t for t in tokens]


def _direcciones_de_ejemplo(direcciones: list[str]) -> list[str]:
    return [d for d in direcciones if _DOMINIOS_DE_EJEMPLO.search(d or "")]


def _advertencia_directorio(verif: dict) -> str:
    fuera = [d for d, etiqueta in (verif or {}).items() if str(etiqueta).startswith("⚠")]
    if not fuera:
        return ""
    return (
        "⚠️ Revisa estas direcciones antes de decir «sí»: no están en el directorio de la empresa (pueden ser "
        f"externas o estar mal escritas): {', '.join(fuera)}"
    )


def _con_advertencia(resultado: str, advertencia: str) -> str:
    if not advertencia:
        return resultado
    try:
        datos = json.loads(resultado)
    except (TypeError, ValueError):
        return resultado
    if not isinstance(datos, dict) or not datos.get("requiere_aprobacion"):
        return resultado
    if isinstance(datos.get("borrador"), str):
        datos["borrador"] = f"{datos['borrador']}\n\n{advertencia}"
    if isinstance(datos.get("vista_previa"), dict) and isinstance(datos["vista_previa"].get("borrador"), str):
        datos["vista_previa"]["borrador"] = f"{datos['vista_previa']['borrador']}\n\n{advertencia}"
    datos["advertencia_destinatarios"] = advertencia
    return json.dumps(datos, ensure_ascii=False)


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


def _encode_url(url: str) -> str:
    return urllib.parse.quote(url, safe="%:/?#[]@!$&'()*+,;=~")


def _graph(method: str, path: str, access_token: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    if access_token == BROKER_TOKEN:
        prefer = (headers or {}).get("Prefer")
        return _broker_graph(method, path, payload, prefer=prefer)
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


def _collect_folders(token: str, consultar=None) -> tuple[list[dict] | None, str | None]:
    consultar = consultar or (lambda ruta: _graph("GET", ruta, token))
    folders: list[dict] = []
    queue: list[tuple[str, str, int]] = [("/me/mailFolders", "", 1)]
    while queue and len(folders) < _MAX_FOLDERS:
        path, prefix, depth = queue.pop(0)
        result = consultar(f"{path}?$top=100&$select={_FOLDER_FIELDS}")
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


def _resolver_carpeta(folder: str) -> tuple[str | None, str | None]:
    raw = str(folder or "inbox").strip() or "inbox"
    key = _fold(raw)
    conocida = _WELL_KNOWN_FOLDERS.get(key) or _FOLDER_ALIASES.get(key)
    if conocida:
        return conocida, None
    if len(raw) >= 40 and " " not in raw:
        return raw, None
    folders, error = _collect_folders("", consultar=lambda ruta: _broker_graph("GET", ruta))
    if folders is None:
        return None, error
    wanted = _fold(raw.strip("/"))
    matches = [f for f in folders if _fold(f["path"]) == wanted] or [f for f in folders if _fold(f["name"]) == wanted]
    if len(matches) == 1:
        return str(matches[0]["id"]), None
    if len(matches) > 1:
        return (
            None,
            f"Hay varias carpetas llamadas '{raw}': {', '.join(f['path'] for f in matches[:10])}. Indica la ruta completa.",
        )
    disponibles = ", ".join(f["path"] for f in folders[:40])
    return None, f"No existe la carpeta '{raw}'. Carpetas disponibles: {disponibles}."


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


BROKER_TOKEN = "__broker__"


def _broker_headers() -> tuple[str, dict, str | None]:
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    headers = {
        "X-HermesHQ-Agent-ID": os.environ.get("HERMESHQ_AGENT_ID", ""),
        "X-HermesHQ-Agent-Token": os.environ.get("HERMESHQ_AGENT_TOKEN", ""),
        "X-HermesHQ-Attempt-ID": os.environ.get("HERMESHQ_ATTEMPT_ID", ""),
        "X-HermesHQ-Lease-Generation": os.environ.get("HERMESHQ_LEASE_GENERATION", ""),
    }
    tarea = None
    raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    if raw:
        try:
            tarea = str((json.loads(raw) or {}).get("task_id") or "").strip() or None
        except (json.JSONDecodeError, ValueError):
            tarea = None
    return base_url, headers, tarea


def _token_herramienta(user_id: str) -> tuple[str | None, str]:
    if os.environ.get("HERMESHQ_APROBACION_CENTRAL", "").strip() == "1":
        return _broker_token_check(user_id)
    return _get_m365_token(user_id)


def _broker_token_check(user_id: str) -> tuple[str | None, str]:
    base_url, headers, tarea = _broker_headers()
    if not base_url or not headers["X-HermesHQ-Agent-ID"] or not headers["X-HermesHQ-Agent-Token"] or not tarea:
        return None, "HermesHQ internal control no configurado o sin tarea activa."
    return BROKER_TOKEN, ""


def _broker_graph(metodo: str, ruta: str, cuerpo_json: dict | None = None, prefer: str | None = None) -> dict:
    base_url, headers, tarea = _broker_headers()
    if not base_url or not headers["X-HermesHQ-Agent-ID"] or not headers["X-HermesHQ-Agent-Token"] or not tarea:
        return {"error": "HermesHQ internal control no configurado o sin tarea activa."}
    body: dict = {"tarea_id": tarea, "metodo": metodo.upper(), "ruta": ruta}
    if prefer:
        body["prefer"] = prefer
    if cuerpo_json is not None:
        body["cuerpo_json"] = cuerpo_json
    req = urllib.request.Request(
        f"{base_url}/control/m365/graph",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            res = json.loads(resp.read().decode("utf-8") or "{}")
            if "base64" in res:
                return {"_base64": res["base64"]}
            return res.get("json") or {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detalle = json.loads(raw).get("detail", raw)
        except (json.JSONDecodeError, ValueError, AttributeError):
            detalle = raw
        motivo = detalle.get("motivo") if isinstance(detalle, dict) else str(detalle)
        if motivo == "mensaje_no_autorizado":
            return {
                "error": "Ese correo no está en el buzón del usuario: el id no existe o quedó mal copiado "
                "(mensaje_no_autorizado). No es un problema de permisos. Usa el «#N» de la lista mostrada como "
                "message_id o vuelve a listar y usa el id exacto.",
                "_graph_code": "itemNotFound",
            }
        return {"error": f"Acceso denegado por la política de correo de HermesHQ ({motivo})."}
    except Exception as exc:  # noqa: BLE001  # HTTP request catch-all
        return {"error": f"No se pudo consultar el correo a través de HermesHQ: {exc}"}


_BARRIDO: dict = {}


def _barrido() -> dict:
    clave = hashlib.sha1(os.environ.get("HERMESHQ_TASK_PAYLOAD", "").encode("utf-8")).hexdigest()
    if _BARRIDO.get("clave") != clave:
        _BARRIDO.clear()
        _BARRIDO.update({"clave": clave, "carpetas": None, "listadas": {}})
    return _BARRIDO


def _anotar_carpetas(folders: list[dict]) -> None:
    _barrido()["carpetas"] = [f for f in folders if (f.get("unread") or 0) > 0]


def _ruta_listada(pedida: str, resuelta: str, carpetas: list[dict]) -> str:
    clave = _fold(pedida.strip("/"))
    for f in carpetas:
        conocida = _WELL_KNOWN_FOLDERS.get(_fold(f["name"])) or _FOLDER_ALIASES.get(_fold(f["name"]))
        if f.get("id") == resuelta or _fold(f["path"]) == clave or (conocida and conocida == resuelta):
            return f["path"]
    coinciden = [f["path"] for f in carpetas if _fold(f["name"]) == clave]
    return coinciden[0] if len(coinciden) == 1 else pedida


def _avance_del_barrido(pedida: str, resuelta: str, since: str | None, cantidad: int) -> dict | None:
    estado = _barrido()
    carpetas = estado["carpetas"]
    if carpetas is None:
        return None
    ruta = _ruta_listada(pedida, resuelta, carpetas)
    llave = f"{_fold(ruta)}|{since or ''}"
    repetida = llave in estado["listadas"]
    estado["listadas"][llave] = (ruta, cantidad)
    listadas = {_fold(r) for r, _ in estado["listadas"].values()}
    pendientes = [f["path"] for f in carpetas if _fold(f["path"]) not in listadas]
    avance = {
        "carpetas_con_no_leidos": len(carpetas),
        "ya_listadas": [f"{r} ({n})" for r, n in estado["listadas"].values()],
        "pendientes": pendientes,
    }
    if repetida:
        avance["aviso"] = (
            f"«{ruta}» ya se listó en esta tarea con este mismo 'since': no la vuelvas a listar; usa lo que ya tienes"
            + (" y sigue con las pendientes." if pendientes else ".")
        )
    if not pendientes:
        avance["siguiente"] = (
            "Ya listaste todas las carpetas con no leídos: arma la respuesta con lo listado; para un resumen no hace "
            "falta abrir cada correo con ms365_mail_get."
        )
    return avance


def _list_emails_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    count = min(int(args.get("count") or 10), 50)
    folder, folder_error = _resolver_carpeta(str(args.get("folder") or "inbox"))
    if folder_error:
        return json.dumps({"success": False, "error": folder_error}, ensure_ascii=False)
    since, since_error = _parse_since(args.get("since"), hora_de_chile=True)
    if since_error:
        return json.dumps({"success": False, "error": since_error})
    if since and since > (datetime.now(UTC) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"):
        return json.dumps(
            {
                "success": False,
                "error": f"since ({args.get('since')} = {_desde_chile(since)}) queda en el futuro: así no aparece "
                "ningún correo. Revisa la fecha («desde ayer a las 18:00» es el día anterior) y repite con la hora de "
                "Chile sin zona, por ejemplo 2026-11-12T18:00:00.",
            },
            ensure_ascii=False,
        )
    unread_only = bool(args.get("unread_only"))
    preview_chars = max(0, min(int(args.get("preview_chars") or _DEFAULT_PREVIEW_CHARS), 255))
    filters = []
    if since or unread_only:
        filters.append(f"receivedDateTime ge {since or '1900-01-01T00:00:00Z'}")
    if unread_only:
        filters.append("isRead eq false")
    filter_clause = f"&$filter={urllib.parse.quote(' and '.join(filters))}" if filters else ""
    path = f"/me/mailFolders/{folder}/messages?$top={count}{filter_clause}"
    result = _broker_graph("GET", path)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    messages = result.get("value", [])
    _cap_previews(messages, preview_chars)
    _con_hora_local(messages)
    _remember_ids(messages)
    sin_id = _anotar_ids_cortos(messages)
    respuesta = {"success": True, "count": len(messages), "ahora_chile": _ahora_chile(), "messages": messages}
    if sin_id:
        respuesta["aviso_ids"] = _AVISO_IDS_CORTOS
    if since:
        respuesta["desde_chile"] = _desde_chile(since)
    if unread_only:
        avance = _avance_del_barrido(str(args.get("folder") or "inbox"), folder, since, len(messages))
        if avance:
            respuesta["barrido"] = avance
    return json.dumps(respuesta, ensure_ascii=False)


def _list_folders_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    folders, error = _collect_folders(token)
    if folders is None:
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    _anotar_carpetas(folders)
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


_MAX_PEOPLE_PER_CALL = 5


def _find_person_tool(args: dict, **_kwargs) -> str:
    names = args.get("names")
    if isinstance(names, list) and names:
        searches = []
        for person in [str(n) for n in names if str(n or "").strip()][:_MAX_PEOPLE_PER_CALL]:
            outcome = json.loads(_find_person_tool({"name": person, "max_results": args.get("max_results")}))
            searches.append({"buscado": person, "resultado": outcome})
        if not searches:
            return json.dumps({"success": False, "error": "Se requiere names (lista de nombres) o name."})
        return json.dumps(
            {"success": any(x["resultado"].get("success") for x in searches), "busquedas": searches}, ensure_ascii=False
        )
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
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
    fuentes = (
        (f"/me/messages?$search=%22{quoted}%22&$top=40&$select={fields}", "correo"),
        (f"/me/mailFolders/sentitems/messages?$search=%22{quoted}%22&$top=40&$select={fields}", "enviados"),
    )
    if token == BROKER_TOKEN:
        fuentes = (
            (f"/me/mailFolders/inbox/messages?$search=%22{quoted}%22&$top=25", "correo"),
            (f"/me/mailFolders/sentitems/messages?$search=%22{quoted}%22&$top=25", "enviados"),
        )
    for path, source in fuentes:
        result = _graph("GET", path, token)
        if "error" in result:
            errors.append(str(result["error"]))
            continue
        for message in result.get("value", result.get("items", [])):
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
        with _bloqueo_archivo(path):
            try:
                previos = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previos = []
            if not isinstance(previos, list):
                previos = []
            mezcla = [i for i in previos if i not in nuevos] + nuevos
            _cu_escribir_atomico(path, json.dumps(mezcla[-_ID_CACHE_MAX:]))
    except (OSError, EstadoOcupado):
        pass


def _known_ids() -> set:
    try:
        datos = json.loads(_id_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(i) for i in datos if i} if isinstance(datos, list) else set()


def _resolve_message_id(given: str) -> str:
    if _DISPLAY_ID_RE.fullmatch(given or ""):
        return _id_de_la_referencia_del_chat(given) or given
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
    if len(cercanos) == 1:
        return cercanos[0]
    return _id_con_tramo_medio_perdido(given, conocidos) or given


def _id_con_tramo_medio_perdido(given: str, conocidos: list) -> str | None:
    limpio = given.strip().strip("\"'`<>").rstrip("=")
    if len(limpio) < 40 or not re.fullmatch(r"[A-Za-z0-9_\-/+%]+", limpio):
        return None
    cola = limpio[-16:]
    cabeza = limpio[:24]
    elegidos = [
        i
        for i in conocidos
        if isinstance(i, str) and len(i) > len(limpio) and i.startswith(cabeza) and i.rstrip("=").endswith(cola)
    ]
    return elegidos[0] if len(elegidos) == 1 else None


def _bad_id_message(result: dict) -> str | None:
    low = f"{result.get('_graph_code') or ''} {result.get('error') or ''}".lower()
    if "malformed" in low or "id is invalid" in low:
        return (
            "El id del correo no es válido (está incompleto o quedó de una lista anterior). No reintentes con el "
            "mismo id: vuelve a listar con ms365_mail_list o ms365_mail_search y usa el id EXACTO que devuelven."
        )
    return None


_REAL_ID_PREFIJOS = ("AAMk", "AQMk", "AAkA", "AAMQ")


def _id_rechazable(message_id: str) -> str | None:
    mid = str(message_id or "").strip()
    conocidos = _known_ids()
    if not conocidos or mid in conocidos:
        return None
    parece_real = (
        len(mid) >= 70 and mid[:4] in _REAL_ID_PREFIJOS and re.fullmatch(r"[A-Za-z0-9_\-/+=]+", mid) is not None
    )
    if parece_real:
        return None
    return (
        "Ese message_id no salió de una búsqueda ni de un listado (parece inventado o truncado). Búscalo con "
        "ms365_mail_search o ms365_mail_list y usa el id EXACTO que devuelven; no lo escribas de memoria."
    )


def _firma_manifest_path() -> Path:
    base = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd())
    return base / "work" / "firma" / "manifest.json"


def _firma_config(nombre) -> tuple[list[dict], str | None, str | None]:
    nombre = str(nombre or "").strip()
    if not nombre:
        return [], None, None
    try:
        manifiesto = json.loads(_firma_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (
            [],
            None,
            (
                "Pediste una firma pero no hay firmas configuradas (falta work/firma/manifest.json). Pásala con "
                "inline_images o pide configurar la firma."
            ),
        )
    if not isinstance(manifiesto, dict) or nombre not in manifiesto:
        disponibles = sorted(manifiesto) if isinstance(manifiesto, dict) else []
        return [], None, f"No encontré la firma '{nombre}' en work/firma/manifest.json. Disponibles: {disponibles}."
    entrada = manifiesto.get(nombre)
    html: str | None = None
    if isinstance(entrada, list):
        items = entrada
    elif isinstance(entrada, dict):
        items = entrada.get("imagenes") or entrada.get("images") or []
        if entrada.get("html"):
            html = str(entrada["html"])
        elif entrada.get("html_path"):
            html, html_err = _leer_firma_html(entrada["html_path"])
            if html_err:
                return [], None, html_err
    else:
        return [], None, f"La firma '{nombre}' en work/firma/manifest.json está mal definida."
    imgs, err = _load_inline_images(items)
    if err:
        return [], None, err
    return imgs, html, None


def _leer_firma_html(raw_path) -> tuple[str | None, str | None]:
    raw = str(raw_path or "").strip()
    if not raw:
        return None, "html_path de la firma vacío."
    base = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()).resolve()
    candidate = Path(raw)
    path = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if base != path and base not in path.parents:
        return None, f"El HTML de la firma '{raw}' está fuera del directorio de trabajo del agente."
    try:
        return path.read_text(encoding="utf-8"), None
    except OSError:
        return None, f"No pude leer el HTML de la firma en '{raw}'."


_CORREO_EN_FIRMA = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _firma_de_otra_persona(token: str, nombre: str | None, firma_html: str | None) -> str | None:
    if (nombre or "").strip().lower() != FIRMA_POR_DEFECTO or not firma_html:
        return None
    correos = {c.lower().rstrip(".") for c in _CORREO_EN_FIRMA.findall(firma_html)}
    if not correos:
        return None
    yo = _graph("GET", "/me?$select=mail,userPrincipalName", token)
    if not isinstance(yo, dict) or "error" in yo:
        return None
    propios = {str(yo.get(k) or "").lower() for k in ("mail", "userPrincipalName")} - {""}
    dominios = {p.split("@", 1)[1] for p in propios}
    raiz = {".".join(d.split(".")[-2:]) for d in dominios}
    de_la_organizacion = {c for c in correos if ".".join(c.split("@", 1)[1].split(".")[-2:]) in raiz}
    if not de_la_organizacion or de_la_organizacion & propios:
        return None
    return (
        f"La firma «{nombre}» configurada para este agente es de otra persona ({', '.join(sorted(de_la_organizacion))}), "
        f"no de quien envía ({', '.join(sorted(propios))}). No envío con la firma de otra persona: díselo al usuario y "
        "pregúntale si quiere enviarlo sin firma (firma='ninguna') o configurar la suya."
    )


def _firma_inline_images(nombre) -> tuple[list[dict], str | None]:
    imgs, _html, err = _firma_config(nombre)
    return imgs, err


def _aplicar_firma_html(body: str, firma_imgs: list[dict], firma_html: str | None) -> str:
    if not firma_html:
        return body
    cids = [a.get("contentId") for a in firma_imgs]
    if any(cid and f"cid:{cid}" in body for cid in cids):
        return body
    return f"{body}\n{firma_html}" if body else firma_html


def _merge_inline(firma: list[dict], modelo: list[dict]) -> list[dict]:
    por_cid = {a.get("contentId"): a for a in modelo}
    for a in firma:
        por_cid[a.get("contentId")] = a
    return list(por_cid.values())


def _fetch_message(token: str, message_id: str, body_format: str, max_chars: int) -> tuple[dict | None, str | None]:
    message_id = _resolve_message_id(message_id)
    headers = {"Prefer": 'outlook.body-content-type="text"'} if body_format == "text" else None
    result = _graph("GET", f"/me/messages/{message_id}?$select={_GET_FIELDS}", token, headers=headers)
    if "error" in result:
        return None, _bad_id_message(result) or result["error"]
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


def _id_de_la_referencia_del_chat(numero) -> str | None:
    coincide = re.search(r"\d+", str(numero or ""))
    referencia = (_payload_tarea().get("metadata") or {}).get("referencia_correo_chat") or {}
    if not coincide or not isinstance(referencia, dict):
        return None
    for correo in referencia.get("correos") or []:
        if isinstance(correo, dict) and str(correo.get("posicion")) == str(int(coincide.group())):
            return str(correo.get("message_id") or "") or None
    return None


def _get_email_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _broker_token_check(user_id)
    if not token:
        return _auth_error(err)
    message_id = str(args.get("message_id") or "").strip()
    if not message_id:
        return json.dumps({"success": False, "error": "Se requiere message_id."})
    pedido_como_id = _cu_norm_display(message_id) if _DISPLAY_ID_RE.fullmatch(message_id) else None
    if _DISPLAY_ID_RE.fullmatch(message_id):
        del_chat = _id_de_la_referencia_del_chat(message_id)
        resuelto = {"message_id": del_chat} if del_chat else _cu_resolver_id({"display_id": message_id})
        if not resuelto.get("message_id"):
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"{_cu_norm_display(message_id)} no es un id de correo que pueda abrir. Usa el message_id "
                        "exacto: el que trae la [Referencia: …] de este mensaje o el que devolvió ms365_mail_list / "
                        "ms365_mail_search; si no lo tienes, vuelve a listar. No inventes a qué correo corresponde."
                    ),
                },
                ensure_ascii=False,
            )
        message_id = resuelto["message_id"]
    body_format = str(args.get("format") or "text").strip().lower()
    if body_format not in ("text", "html"):
        return json.dumps({"success": False, "error": "format debe ser text o html."})
    default_chars = _GET_HTML_DEFAULT_CHARS if body_format == "html" else _GET_TEXT_DEFAULT_CHARS
    max_chars = _int_arg(args.get("max_chars"), default_chars, _GET_MIN_CHARS, _GET_MAX_CHARS)
    message, error = _fetch_message(token, message_id, body_format, max_chars)
    if error:
        return json.dumps({"success": False, "error": error})
    _remember_ids([message])
    sin_id = _anotar_ids_cortos([message])
    salida = {"success": True, "message": message}
    if pedido_como_id:
        salida["nota_id"] = (
            f"{pedido_como_id} es ESTE correo: «{message.get('subject') or ''}». No se lo atribuyas a otro asunto."
        )
    elif sin_id:
        salida["aviso_ids"] = _AVISO_IDS_CORTOS
    return json.dumps(salida, ensure_ascii=False)


_GET_MANY_MAX = 10
_GET_MANY_DEFAULT_CHARS = 1500


def _get_emails_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    raw_ids = args.get("message_ids")
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    ids = []
    for item in raw_ids or []:
        item = str(item or "").strip()
        if _DISPLAY_ID_RE.fullmatch(item):
            item = _id_de_la_referencia_del_chat(item) or item
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
    _remember_ids(messages)
    payload = {"success": True, "count": len(messages), "messages": messages}
    if omitted:
        payload["omitted_ids"] = omitted
        payload["note"] = f"Máximo {_GET_MANY_MAX} correos por llamada; pide el resto en otra llamada."
    return json.dumps(payload, ensure_ascii=False)


_APPROVAL_TTL_SECONDS = 4 * 60 * 60
_MAX_DOWNLOAD_ATTACHMENTS = 10
_MAX_DOWNLOAD_ATTACHMENT_BYTES = 10 * 1024 * 1024


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


_MARCAR_DECLARACIONES = (
    re.compile(r"\bmarc\w*(?:\s+[^\s.;:!?\n]+){0,6}?\s+como\s+leid[oa]s?\b"),
    re.compile(r"\bmark\w*(?:\s+[^\s.;:!?\n]+){0,6}?\s+as\s+read\b"),
)
_MARCAR_NEGACION_PREVIA = re.compile(
    r"(?:^|[^\w])(?:no|ni|sin|nunca|jamas|evita\w*|prohib\w*|never|not|without|don'?t|do\s+not)"
    r"(?:\s+[^\s.,;:!?\n]+)?\s*$"
)
_MARCAR_NEGACION_INTERNA = re.compile(r"\b(?:no|ni|sin|nunca|jamas|never|not|without)\b")
_MARCAR_CLAVE_METADATA = "marcar_leidos_autorizado"


def _declara_marcar_leidos(texto) -> bool:
    normal = "".join(c for c in unicodedata.normalize("NFKD", str(texto or "")) if not unicodedata.combining(c)).lower()
    for patron in _MARCAR_DECLARACIONES:
        for hallazgo in patron.finditer(normal):
            previo = normal[max(0, hallazgo.start() - 40) : hallazgo.start()]
            if not _MARCAR_NEGACION_PREVIA.search(previo) and not _MARCAR_NEGACION_INTERNA.search(hallazgo.group(0)):
                return True
    return False


def _marcado_autorizado_tarea() -> bool:
    if not _modo_automatico():
        return True
    try:
        payload = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    meta = payload.get("metadata") or {}
    if isinstance(meta, dict) and _MARCAR_CLAVE_METADATA in meta:
        return meta.get(_MARCAR_CLAVE_METADATA) is True
    return _declara_marcar_leidos(payload.get("prompt"))


_MARCAR_NO_DECLARADO = (
    "No marqué ningún correo: esta es una tarea automática (programada o en segundo plano) y su definición no pide "
    "marcar correos como leídos. Informa los correos sin marcarlos; para que una tarea programada marque, su "
    "instrucción debe decirlo explícitamente (p. ej. «marca como leídos los correos del informe»)."
)


def _previews_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".mail_previews.json"


def _leer_previews_crudas(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_previews() -> dict:
    limit = time.time() - _APPROVAL_TTL_SECONDS
    return {
        k: v
        for k, v in _leer_previews_crudas(_previews_path()).items()
        if isinstance(v, dict) and float(v.get("ts") or 0) >= limit
    }


def _save_previews(previews: dict, borrar: str | None = None) -> None:
    try:
        path = _previews_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        limite = time.time() - _APPROVAL_TTL_SECONDS
        combinadas = {
            k: v
            for k, v in {**_leer_previews_crudas(path), **previews}.items()
            if isinstance(v, dict) and float(v.get("ts") or 0) >= limite and k != borrar
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(combinadas), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _conversacion_actual() -> str:
    meta = _payload_tarea().get("metadata") or {}
    if not isinstance(meta, dict):
        return ""
    for campo in ("thread_id", "public_session_id"):
        valor = str(meta.get(campo) or "").strip()
        if valor:
            return valor
    return ""


def _clave_vista(fingerprint: str) -> str:
    conversacion = _conversacion_actual()
    return f"{fingerprint}:{hashlib.sha256(conversacion.encode()).hexdigest()[:24]}" if conversacion else fingerprint


def _action_fingerprint(user_id: str, *parts) -> str:
    return hashlib.sha256(
        "|".join([user_id, *[json.dumps(p, sort_keys=True, default=str) for p in parts]]).encode()
    ).hexdigest()


def _huella_adjuntos(adjuntos: list[dict] | None) -> list[list[str]]:
    return sorted(
        [
            str(a.get("contentId") or a.get("name") or ""),
            hashlib.sha256(str(a.get("contentBytes") or "").encode("ascii", "ignore")).hexdigest(),
        ]
        for a in adjuntos or []
    )


APROBACION_TEXTUAL = re.compile(
    r"^\s*(s[ií][,.]?\s*)?(env[ií]a|env[ií]alo|env[ií]ala|enviar|manda(lo|la)?|reenv[ií]a(lo|la|le)?|reenviar|"
    r"resp[oó]nde(lo|le)?|contesta(lo|le)?)\b",
    re.IGNORECASE,
)


def _aprobacion_en_el_mensaje() -> bool:
    limpio = _mensaje_limpio()
    if not limpio or len(limpio) > 160 or "?" in limpio or "¿" in limpio or _anula_la_aprobacion(limpio):
        return False
    return bool(APROBACION_TEXTUAL.match(limpio))


def _payload_tarea() -> dict:
    try:
        datos = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return {}
    return datos if isinstance(datos, dict) else {}


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
    "envia",
    "envialo",
    "enviala",
    "enviar",
    "enviarlo",
    "enviarla",
    "manda",
    "mandalo",
    "mandala",
    "mandar",
    "yes",
    "procede",
    "hazlo",
    "autorizo",
    "apruebo",
    "aprobado",
    "reenvia",
    "reenvialo",
    "reenviala",
    "reenviale",
    "reenviar",
    "reenviarlo",
    "reenviarla",
    "responde",
    "respondelo",
    "respondele",
    "responder",
    "contesta",
    "contestalo",
    "contestale",
    "enviale",
    "mandale",
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
    "el",
    "correo",
    "ahora",
    "con",
    "todo",
    "todos",
    "igual",
}


_BLOQUE_RESPUESTA_CORTA = re.compile(r"(?s)\[Respuesta corta a tu último mensaje:.*?no a temas anteriores\.\]\s*")
_BLOQUES_DEL_SISTEMA = re.compile(
    r"(?s)\[(?:FORMATO PEDIDO EN ESTE MENSAJE|Hora actual|AVISO DEL SISTEMA|Archivos adjuntos|Archivos de turnos"
    r"|B[uú]squeda en SharePoint)[^\]]*(?:\]|$)"
)
_LINEA_ARCHIVO_PREVIO = re.compile(r"(?m)^\s*- .+ \(hace \d+ min\)\s*$")
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


def _mensaje_limpio(mensaje: str | None = None) -> str:
    texto = str(_payload_tarea().get("mensaje") or "") if mensaje is None else str(mensaje)
    texto = _BLOQUE_RESPUESTA_CORTA.sub(" ", texto)
    texto = _BLOQUES_DEL_SISTEMA.sub(" ", texto)
    texto = _LINEA_ARCHIVO_PREVIO.sub(" ", texto)
    return texto.strip()


def _palabras_planas(texto: str) -> list[str]:
    return re.findall(r"[a-z]+", unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower())


def _anula_la_aprobacion(texto: str) -> bool:
    for palabra in _palabras_planas(texto):
        for forma in {palabra, re.sub(r"(.)\1{2,}", r"\1", palabra), re.sub(r"(.)\1+", r"\1", palabra)}:
            if forma in _PALABRAS_QUE_ANULAN or forma.startswith(_PREFIJOS_QUE_ANULAN):
                return True
    return False


def es_aprobacion_pura(mensaje: str | None = None) -> bool:
    texto = _mensaje_limpio(mensaje)
    if not texto or len(texto) > 60 or "?" in texto or "¿" in texto or _anula_la_aprobacion(texto):
        return False
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()
    palabras = re.findall(r"[a-z]+", sin_tildes)
    if not palabras or not any(p in _PALABRAS_APROBACION for p in palabras):
        return False
    return all(p in _PALABRAS_APROBACION or p in _PALABRAS_RELLENO for p in palabras)


def _usuario_aprueba_ahora() -> bool:
    return es_aprobacion_pura()


def _borrador_ya_mostrado(preview: dict) -> bool:
    texto = str(_payload_tarea().get("respuesta_anterior") or "").lower()
    if not texto:
        return False
    correos: list[str] = []
    for clave in ("para", "cc", "cco", "responde_a", "tambien_a"):
        valor = preview.get(clave)
        if isinstance(valor, str):
            valor = [valor]
        correos += [str(c) for c in (valor or []) if c]
    if not correos or any(c.lower() not in texto for c in correos):
        return False
    asunto = re.sub(r"\s+", " ", str(preview.get("asunto") or "")).strip().lower()
    asunto = re.sub(r"^(re|rv|fw|fwd)\s*:\s*", "", asunto)
    if not asunto or asunto not in re.sub(r"\s+", " ", texto):
        return False
    contenido = "".join(
        _palabras_y_numeros(str(preview.get("cuerpo") or preview.get("texto") or preview.get("comentario") or ""))
    )
    return not contenido or contenido in "".join(_palabras_y_numeros(texto))


def _palabras_y_numeros(texto: str) -> list[str]:
    plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()
    return re.findall(r"[a-z0-9]+", plano)


SIN_FIRMA = {"ninguna", "sin firma", "sin", "no", "none", "false"}
FIRMA_POR_DEFECTO = "personal"
_ETIQUETA_HTML = re.compile(r"</?(p|br|div|table|tr|td|span|b|strong|i|em|ul|ol|li|h[1-6]|a|img|font)\b", re.I)


_SIN_FIRMA_DEL_USUARIO = re.compile(
    r"\b(?:sin\s+(?:mi\s+|la\s+|ninguna\s+)?firma|firma\s+ninguna|no\s+(?:uses|pongas|agregues|incluyas|lleves)\s+(?:mi\s+|la\s+)?firma)\b"
)


def _usuario_pidio_sin_firma() -> bool:
    payload = _payload_tarea()
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    textos = [metadata.get("mensaje_usuario"), metadata.get("pedido_del_usuario"), payload.get("mensaje")]
    for texto in textos:
        plano = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
        if _SIN_FIRMA_DEL_USUARIO.search(plano):
            return True
    return False


def _nombre_firma(args: dict) -> str | None:
    pedido = str(args.get("firma") or "").strip()
    if pedido.lower() in SIN_FIRMA and pedido:
        return None
    if pedido:
        return pedido
    if _usuario_pidio_sin_firma():
        return None
    try:
        manifiesto = json.loads(_firma_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return FIRMA_POR_DEFECTO if isinstance(manifiesto, dict) and FIRMA_POR_DEFECTO in manifiesto else None


def _texto_a_html(texto: str) -> str:
    if not texto or _ETIQUETA_HTML.search(texto):
        return texto
    parrafos = [p for p in re.split(r"\n\s*\n", texto.strip()) if p.strip()]
    return "".join(f"<p>{html_escape(p.strip()).replace(chr(10), '<br>')}</p>" for p in parrafos)


_DESPEDIDA = (
    r"(saludos( cordiales)?|saluda (atentamente|cordialmente)|atentamente|cordialmente|un saludo|saludos atentos)"
)
_DESPEDIDA_FINAL = re.compile(r"(?is)(\n|<br\s*/?>|<(p|div)[^>]*>)?\s*" + _DESPEDIDA + r"\s*[,.]?\s*(</(p|div)>)?\s*$")


def _quitar_despedida_repetida(body: str, firma_html: str | None) -> str:
    if not body or not firma_html:
        return body
    primera = next((x.strip() for x in _html_to_text(firma_html).split("\n") if x.strip()), "")
    if not re.fullmatch(_DESPEDIDA + r"\s*[,.]?", primera, flags=re.I):
        return body
    return _DESPEDIDA_FINAL.sub("", body).rstrip()


_MARCADOR_IMAGEN = re.compile(r"^\[(logo|banner|imagen|firma)[^\]]*\]", re.I)


def _html_a_texto_con_parrafos(cuerpo_html: str) -> str:
    import html as _html

    crudo = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", "", cuerpo_html or "")
    crudo = re.sub(r"(?i)<br\s*/?>", "\n", crudo)
    crudo = re.sub(r"(?i)</(p|div|li|tr|h[1-6])\s*>", "\n\n", crudo)
    texto = _html.unescape(re.sub(r"<[^>]+>", "", crudo))
    texto = re.sub(r"[ \t]+\n", "\n", texto)
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


def _norm_linea(linea: str) -> str:
    return re.sub(r"\s+", " ", linea).strip().lower()


def _quitar_firma_pegada(body: str, firma_html: str | None) -> str:
    if not body or not firma_html:
        return body
    es_html = bool(_ETIQUETA_HTML.search(body))
    original = body
    if es_html:
        body = _html_a_texto_con_parrafos(body)
    firma = {_norm_linea(x) for x in _html_to_text(firma_html).split("\n") if len(_norm_linea(x)) >= 4}
    lineas = body.split("\n")
    coinciden = [i for i, x in enumerate(lineas) if _norm_linea(x) in firma or _MARCADOR_IMAGEN.match(x.strip())]
    if len(coinciden) < 3:
        return original
    resto = lineas[: coinciden[0]]
    despedida = re.compile(_DESPEDIDA + r"\s*[,.]?", re.I)
    no_vacias = [i for i, x in enumerate(resto) if x.strip()]
    while len(no_vacias) >= 2 and all(despedida.fullmatch(resto[i].strip()) for i in no_vacias[-2:]):
        resto = resto[: no_vacias[-1]]
        no_vacias = no_vacias[:-1]
    limpio = "\n".join(resto).rstrip()
    return _texto_a_html(limpio) if es_html else limpio


def _cids_sin_imagen(body: str, adjuntos: list[dict]) -> list[str]:
    disponibles = {str(a.get("contentId") or "") for a in adjuntos}
    return sorted({c for c in re.findall(r"cid:([\w.@-]+)", body or "", flags=re.I) if c not in disponibles})


def _borrador_canonico(encabezados: list[tuple[str, str]], cuerpo_html: str, firma: str | None) -> str:
    lineas = [f"**{etiqueta}:** {valor}" for etiqueta, valor in encabezados if valor]
    texto = _html_a_texto_con_parrafos(cuerpo_html)
    parrafos = [p.strip() for p in re.split(r"\n\s*\n", texto) if p.strip()]
    pie = f"_Firma: {firma} (la agrega el servidor al enviar)_" if firma else "_Sin firma_"
    return "\n".join(lineas) + "\n\n---\n\n" + "\n\n".join(parrafos) + "\n\n---\n\n" + pie


AVISO_NO_ENVIADO = "El correo NO se envió"
_PENDIENTE_TTL = 4 * 3600


def _pendiente_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".mail_pendiente.json"


def _guardar_pendiente(herramienta: str, args: dict, preview: dict) -> None:
    datos = {
        "herramienta": herramienta,
        "args": {k: v for k, v in args.items() if k != "aprobado_por_el_usuario"},
        "task": _current_task_id(),
        "conv": _conversacion_actual(),
        "ts": time.time(),
        "preview": {
            k: preview.get(k)
            for k in ("para", "cc", "cco", "responde_a", "tambien_a", "asunto", "cuerpo", "texto", "comentario")
        },
    }
    ruta = _pendiente_path()
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta.with_suffix(".tmp")
        tmp.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, ruta)
    except OSError:
        pass


def _borrar_pendiente() -> None:
    try:
        _pendiente_path().unlink()
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


def ejecutar_aprobacion_pendiente() -> dict | None:
    if _aprobacion_central_activa():
        return ejecutar_aprobadas_central()
    if _modo_automatico() or not es_aprobacion_pura():
        return None
    pendiente = _leer_pendiente()
    if not pendiente or pendiente.get("task") == _current_task_id():
        return None
    if str(pendiente.get("conv") or "") != _conversacion_actual():
        return None
    anterior = str(_payload_tarea().get("respuesta_anterior") or "")
    if not (_borrador_ya_mostrado(pendiente.get("preview") or {}) or AVISO_NO_ENVIADO in anterior):
        return None
    herramientas = {
        "send": ("ms365_mail_send", _send_email_tool),
        "reply": ("ms365_mail_reply", _reply_email_tool),
        "forward": ("ms365_mail_forward", _forward_email_tool),
    }
    if pendiente.get("herramienta") not in herramientas:
        return None
    nombre, funcion = herramientas[pendiente["herramienta"]]
    args = dict(pendiente.get("args") or {}, aprobado_por_el_usuario=True)
    try:
        resultado = json.loads(funcion(args))
    except Exception as exc:  # noqa: BLE001
        resultado = {"success": False, "error": f"Falló el envío: {exc}"}
    return {"herramienta": nombre, "args": args, "resultado": resultado, "preview": pendiente.get("preview") or {}}


_ENCABEZADO_BORRADOR = re.compile(r"^\s*\**\s*(para|cc|cco|asunto|adjuntos)\s*:?\s*\**\s*:?\s*(.*)$", re.I)
_RESPUESTA_BORRADOR = re.compile(r"(?im)^\s*\**\s*(responde a|en respuesta|re\s*:|rv\s*:|fw\s*:|reenv)")
_solo_vista_previa = False


_CAMPO_EN_LISTA = re.compile(
    r"^\s*(?:[-•]\s*)?\**\s*(para|cc|cco|asunto|adjuntos|cuerpo|mensaje|texto)\s*\**\s*:\s*\**\s*(.*)$", re.I
)
_FIN_DEL_CUERPO = re.compile(r"^\s*(¿|responde\b|confirma\b|_?firma\b)", re.I)


_PROPIO_ESCRITO = re.compile(
    r"^(a\s+)?(ti|usted)(\s+mism[oa])?$|^(tu|su) (propio |propia )?(correo|buz[oó]n|cuenta|direcci[oó]n)$", re.I
)


def _destinatarios_escritos(valor: str) -> list[str]:
    direcciones = _EMAIL_RE.findall(valor or "")
    if direcciones:
        return direcciones
    limpio = re.sub(r"\([^)]*\)", " ", valor or "").strip().strip(".:;,").strip()
    limpio = re.sub(r"\s+", " ", limpio)
    return ["yo"] if limpio and (_YO.match(limpio) or _PROPIO_ESCRITO.match(limpio)) else []


def _borrador_en_lista(texto: str) -> dict | None:
    campos: dict[str, str] = {}
    cuerpo: list[str] | None = None
    for linea in texto.splitlines():
        coincide = _CAMPO_EN_LISTA.match(linea)
        if coincide:
            clave, valor = coincide.group(1).lower(), coincide.group(2).strip().strip("*").strip()
            if clave in ("cuerpo", "mensaje", "texto"):
                cuerpo = [valor] if valor else []
            elif cuerpo is None:
                campos[clave] = valor
            else:
                break
            continue
        if cuerpo is not None:
            if _FIN_DEL_CUERPO.match(linea):
                break
            cuerpo.append(linea)
    if cuerpo is None or campos.get("adjuntos"):
        return None
    texto_cuerpo = "\n\n".join(p.strip() for p in re.split(r"\n\s*\n", "\n".join(cuerpo).strip()) if p.strip())
    para = _destinatarios_escritos(campos.get("para") or "")
    asunto = campos.get("asunto") or ""
    if not para or not asunto or not texto_cuerpo or re.match(r"^(re|rv|fw|fwd)\s*:", asunto, re.I):
        return None
    args: dict = {"to": para, "subject": asunto, "body": texto_cuerpo}
    for clave, destino in (("cc", "cc"), ("cco", "bcc")):
        if campos.get(clave):
            args[destino] = _destinatarios_escritos(campos[clave])
    return args


def args_desde_borrador_escrito(texto: str) -> dict | None:
    if not texto:
        return None
    if _RESPUESTA_BORRADOR.search(texto.split("---")[0]):
        return None
    partes = re.split(r"(?m)^\s*---\s*$", texto)
    if len(partes) < 3:
        return _borrador_en_lista(texto)
    encabezados: dict[str, str] = {}
    for linea in partes[0].splitlines():
        coincide = _ENCABEZADO_BORRADOR.match(linea)
        if coincide:
            encabezados[coincide.group(1).lower()] = coincide.group(2).strip().strip("*").strip()
    if encabezados.get("adjuntos") or _RESPUESTA_BORRADOR.search(partes[0]):
        return None
    para = _destinatarios_escritos(encabezados.get("para") or "")
    asunto = encabezados.get("asunto") or ""
    if re.match(r"^(re|rv|fw|fwd)\s*:", asunto, re.I):
        return None
    cuerpo = "\n\n".join(p.strip() for p in re.split(r"\n\s*\n", partes[1].strip()) if p.strip())
    if not para or not asunto or not cuerpo:
        return None
    args: dict = {"to": para, "subject": asunto, "body": cuerpo}
    if encabezados.get("cc"):
        args["cc"] = _EMAIL_RE.findall(encabezados["cc"])
    if encabezados.get("cco"):
        args["bcc"] = _EMAIL_RE.findall(encabezados["cco"])
    pie = partes[2].lower()
    if "sin firma" in pie:
        args["firma"] = "ninguna"
    else:
        nombrada = re.search(r"firma:\s*([a-záéíóúñ_-]+)", pie)
        if nombrada:
            args["firma"] = nombrada.group(1)
    return args


def vista_previa_desde_borrador_escrito(texto: str, ya_mostrado: bool = False) -> dict | None:
    global _solo_vista_previa
    if _modo_automatico():
        return None
    args = args_desde_borrador_escrito(texto)
    if not args:
        return None
    _solo_vista_previa = True
    try:
        resultado = json.loads(_send_email_tool(dict(args)))
    except Exception:  # noqa: BLE001
        return None
    finally:
        _solo_vista_previa = False
    if not resultado.get("requiere_aprobacion") or not resultado.get("borrador"):
        return None
    if ya_mostrado:
        pendiente = _leer_pendiente()
        if pendiente:
            pendiente["task"] = "borrador-escrito-mostrado"
            try:
                _pendiente_path().write_text(json.dumps(pendiente, ensure_ascii=False), encoding="utf-8")
            except OSError:
                return None
    return {"herramienta": "ms365_mail_send", "args": args, "resultado": resultado}


def _asunto_base(asunto) -> str:
    texto = re.sub(r"\s+", " ", str(asunto or "")).strip().lower()
    while True:
        sin_prefijo = re.sub(r"^(re|rv|fw|fwd)\s*:\s*", "", texto)
        if sin_prefijo == texto:
            return texto
        texto = sin_prefijo


def _envio_cortado_salio(token: str | None, entry: dict, preview: dict) -> tuple[str, str | None]:
    if not token:
        return "incierto", None
    desde = datetime.fromtimestamp(float(entry.get("en_curso") or 0) - 120, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    filtro = urllib.parse.quote(f"sentDateTime ge {desde}")
    enviados = _graph(
        "GET",
        f"/me/mailFolders/sentitems/messages?$filter={filtro}&$orderby=sentDateTime%20desc&$top=50"
        "&$select=subject,toRecipients,sentDateTime",
        token,
    )
    if not isinstance(enviados, dict) or "error" in enviados:
        return "incierto", None
    asunto = _asunto_base(preview.get("asunto"))
    para = sorted(str(c).lower() for c in preview.get("para") or [] if c)
    for m in enviados.get("value") or []:
        destinos = sorted(
            str(((d or {}).get("emailAddress") or {}).get("address") or "").lower() for d in m.get("toRecipients") or []
        )
        if _asunto_base(m.get("subject")) == asunto and destinos == para:
            return "salio", _hora_chile(m.get("sentDateTime"))
    return "no_salio", None


def _approval_gate(
    args: dict, fingerprint: str, preview: dict, herramienta: str | None = None, token: str | None = None
) -> str | None:
    if _solo_vista_previa:
        args = {k: v for k, v in args.items() if k != "aprobado_por_el_usuario"}
    if _modo_automatico():
        return json.dumps(
            {
                "success": False,
                "error": "No puedo completar un envío de correo en modo automático (tarea programada o en segundo "
                "plano): no hay un usuario que apruebe la vista previa. Si debe salir sin intervención, pídele al "
                "dueño que lo configure como una acción con destinatarios pre-aprobados.",
            },
            ensure_ascii=False,
        )
    previews = _load_previews()
    clave = _clave_vista(fingerprint)
    entry = previews.get(clave)
    current = _current_task_id()
    if entry and entry.get("enviado") and not _solo_vista_previa:
        return json.dumps(
            {
                "success": False,
                "ya_enviado": True,
                "error": "Este mismo correo (destinatarios, asunto, cuerpo y adjuntos) ya se envió desde esta "
                "conversación; no lo vuelvo a enviar. Si quiere otro envío, que cambie el texto.",
            },
            ensure_ascii=False,
        )
    if entry and entry.get("en_curso") and not _solo_vista_previa:
        estado, hora = _envio_cortado_salio(token, entry, preview)
        if estado == "salio":
            _consume_approval(fingerprint)
            return json.dumps(
                {
                    "success": False,
                    "ya_enviado": True,
                    "error": "Este correo ya salió en el intento anterior, que se cortó antes de confirmarlo"
                    + (f" (está en Elementos enviados, {hora})" if hora else " (está en Elementos enviados)")
                    + ". No lo vuelvo a enviar.",
                },
                ensure_ascii=False,
            )
        if estado == "incierto":
            return json.dumps(
                {
                    "success": False,
                    "resultado_incierto": True,
                    "error": "El intento anterior de este mismo envío se cortó mientras salía y no pude revisar "
                    "Elementos enviados. No lo vuelvo a enviar para no duplicarlo: revisa tu carpeta Elementos "
                    "enviados antes de pedirlo de nuevo.",
                },
                ensure_ascii=False,
            )
    previa_de_otra_tarea = bool(entry and current and entry.get("task") and entry["task"] != current)
    pura = _usuario_aprueba_ahora()
    orden = _aprobacion_en_el_mensaje()
    real = bool((pura or orden) and (args.get("aprobado_por_el_usuario") or orden) and previa_de_otra_tarea) or (
        pura and _borrador_ya_mostrado(preview)
    )
    if real and not _solo_vista_previa:
        base = entry if (entry and entry.get("task")) else {"task": current}
        _save_previews({clave: {**base, "ts": time.time(), "en_curso": time.time(), "en_curso_tarea": current}})
        return None
    previews[clave] = {"task": current, "ts": time.time()}
    _save_previews(previews)
    if herramienta:
        _guardar_pendiente(herramienta, args, preview)
    borrador = preview.pop("borrador", None)
    result = {
        "success": False,
        "requiere_aprobacion": True,
        "vista_previa": preview,
        "siguiente": "Muéstrale al usuario esta vista previa y pregunta si lo envías; termina tu respuesta ahí y espera "
        "su siguiente mensaje. Cuando diga que sí, vuelve a llamar con aprobado_por_el_usuario=true y EXACTAMENTE los "
        "mismos destinatarios (para/cc/cco), asunto, cuerpo y adjuntos. Si cambias cualquier dato (también el texto), "
        "es una vista previa nueva que el usuario debe volver a aprobar.",
    }
    if borrador:
        result["borrador"] = borrador
        result["siguiente"] = (
            "Copia el campo 'borrador' TAL CUAL en tu respuesta (no lo resumas, no cambies el formato y no escribas "
            "la firma: la pone el servidor) y termina con «¿Lo envío?». Cuando el usuario diga que sí (envía, sí, "
            "ok…), tu respuesta DEBE ser llamar de nuevo a esta herramienta con aprobado_por_el_usuario=true y los "
            "mismos destinatarios, asunto y adjuntos. Nunca digas que se envió sin el resultado success=true."
        )
    if args.get("aprobado_por_el_usuario"):
        result["error"] = (
            "No se envió: el último mensaje del usuario no es una confirmación clara (no dijo «sí», o lo negó, canceló "
            "o pidió esperar). No lo envíes ni digas que se envió: pregúntale si lo envías."
            if previa_de_otra_tarea
            else "No se envió: esta vista previa exacta (destinatarios, asunto, cuerpo y adjuntos) aún no se le mostró "
            "al usuario en un mensaje anterior. No puedes aprobar por él en la misma respuesta."
        )
    return json.dumps(result, ensure_ascii=False)


def _consume_approval(fingerprint: str) -> None:
    _save_previews({_clave_vista(fingerprint): {"enviado": True, "task": _current_task_id(), "ts": time.time()}})
    _borrar_pendiente()


def _resolver_directorio(token: str, correos: list[str]) -> dict:

    def _esc(c: str) -> str:
        return c.replace("'", "''")

    unicos = [c for c in dict.fromkeys(c for c in correos if c)]
    if not unicos:
        return {}
    clausulas = " or ".join(f"mail eq '{_esc(c)}' or userPrincipalName eq '{_esc(c)}'" for c in unicos[:20])
    res = _graph(
        "GET",
        f"/users?$filter={urllib.parse.quote(clausulas, safe='')}&$select=mail,userPrincipalName,displayName",
        token,
    )
    if not isinstance(res, dict) or "error" in res:
        return {}
    encontrados: dict[str, str] = {}
    for u in res.get("value", []) or []:
        for clave in ("mail", "userPrincipalName"):
            valor = str(u.get(clave) or "").lower()
            if valor:
                encontrados[valor] = u.get("displayName") or valor
    etiquetas: dict[str, str] = {}
    for c in unicos:
        nombre = encontrados.get(c.lower())
        etiquetas[c] = f"✓ en directorio: {nombre}" if nombre else "⚠ NO está en el directorio (externo o inexistente)"
    return etiquetas


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
        f"/me/messages/{message_id}?$select=subject,from,toRecipients,receivedDateTime,sentDateTime,hasAttachments",
        token,
    )
    if "error" in message:
        return None, str(message["error"])
    return message, None


_ETIQUETAS_CITA = (("From", "De"), ("Sent", "Enviado"), ("To", "Para"), ("Cc", "CC"), ("Subject", "Asunto"))
_DIAS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES_ES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)
_BLOQUE_CITA = re.compile(r"<div[^>]*id=\"?divRplyFwdMsg\"?[^>]*>.*?</div>", re.I | re.S)


def _fecha_larga_chile(iso: str | None) -> str | None:
    from zoneinfo import ZoneInfo

    try:
        instante = datetime.fromisoformat(str(iso or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=UTC)
    local = instante.astimezone(ZoneInfo(_ZONA_USUARIO))
    return f"{_DIAS_ES[local.weekday()]}, {local.day} de {_MESES_ES[local.month - 1]} de {local.year} {local:%H:%M}"


def _cita_en_espanol(html: str, enviado_iso: str | None) -> str:
    bloque = _BLOQUE_CITA.search(html or "")
    if not bloque:
        return html
    cita = bloque.group(0)
    for ingles, espanol in _ETIQUETAS_CITA:
        cita = re.sub(rf"(<b>\s*){ingles}:(\s*</b>)", rf"\g<1>{espanol}:\g<2>", cita)
    fecha = _fecha_larga_chile(enviado_iso)
    if fecha:
        cita = re.sub(r"(<b>\s*Enviado:\s*</b>)[^<]*", lambda m: f"{m.group(1)} {fecha}", cita, count=1)
    return html[: bloque.start()] + cita + html[bloque.end() :]


def _asunto_reenvio(asunto: str) -> str:
    return "RV: " + re.sub(r"^\s*((rv|fw|fwd)\s*:\s*)+", "", str(asunto or ""), flags=re.I)


def _forward_email_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    message_id = _resolve_message_id(str(args.get("message_id") or "").strip())
    to, invalid_to = _parse_recipients(_reemplazar_yo(args.get("to"), token))
    cc, invalid_cc = _parse_recipients(_reemplazar_yo(args.get("cc"), token))
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
    rechazo = _id_rechazable(message_id)
    if rechazo:
        return json.dumps({"success": False, "error": rechazo}, ensure_ascii=False)
    message, error = _message_for_action(token, message_id)
    if message is None:
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    attachments, _ = _list_attachments(token, message_id)
    if _aprobacion_central_activa():
        contenido = {
            "cuenta_emisora": f"m365:{user_id}",
            "mensaje_origen_id": message_id,
            "para": [r["emailAddress"]["address"] for r in to],
            "cc": [r["emailAddress"]["address"] for r in cc],
            "asunto": re.sub(r"^(?!(rv|fw|fwd)\s*:)", "RV: ", str(message.get("subject") or ""), flags=re.I),
            "comentario": comment,
            "adjuntos_originales": [str(a.get("name") or "") for a in attachments or [] if not a.get("isInline")],
        }
        return _enviar_central(args, contenido, [], "forward", TIPO_REENVIAR_CENTRAL)
    preview = {
        "accion": "reenviar",
        "asunto": _asunto_reenvio(message.get("subject")),
        "de": ((message.get("from") or {}).get("emailAddress") or {}).get("address"),
        "fecha": message.get("receivedDateTime"),
        "fecha_chile": _hora_chile(message.get("receivedDateTime")),
        "para": [r["emailAddress"]["address"] for r in to],
        "cc": [r["emailAddress"]["address"] for r in cc],
        "comentario": comment or None,
        "adjuntos_e_imagenes_que_viajan": [_attachment_summary(a) for a in attachments or []],
        "nota": "Se reenvía el correo ORIGINAL completo: conserva sus adjuntos e imágenes incrustadas.",
    }
    verif = (
        {}
        if args.get("aprobado_por_el_usuario")
        else _resolver_directorio(token, [r["emailAddress"]["address"] for r in to + cc])
    )
    if verif:
        preview["verificacion_directorio"] = verif
    fingerprint = _action_fingerprint(
        user_id,
        "forward",
        message_id,
        [r["emailAddress"]["address"] for r in to],
        [r["emailAddress"]["address"] for r in cc],
        comment,
    )
    gate = _approval_gate(args, fingerprint, preview, "forward", token)
    if gate is not None:
        return gate
    draft = _graph("POST", f"/me/messages/{message_id}/createForward", token, {"toRecipients": to, "comment": comment})
    if "error" in draft and not cc and "403" in str(draft["error"]):
        sent = _graph("POST", f"/me/messages/{message_id}/forward", token, {"toRecipients": to, "comment": comment})
    elif "error" in draft:
        detail = str(draft["error"])
        if "403" in detail:
            detail += " Para reenviar con copia falta el permiso Mail.ReadWrite: el usuario debe reconectar su cuenta Microsoft 365."
        return json.dumps({"success": False, "error": detail}, ensure_ascii=False)
    else:
        cambios = {"subject": _asunto_reenvio(message.get("subject"))}
        if cc:
            cambios["ccRecipients"] = cc
        actual = _graph("GET", f"/me/messages/{draft.get('id')}?$select=body", token)
        previo = (actual.get("body") or {}).get("content") or "" if isinstance(actual, dict) else ""
        if previo:
            enviado = message.get("sentDateTime") or message.get("receivedDateTime")
            cambios["body"] = {"contentType": "HTML", "content": _cita_en_espanol(previo, enviado)}
        patched = _graph("PATCH", f"/me/messages/{draft.get('id')}", token, cambios)
        sent = _graph("POST", f"/me/messages/{draft.get('id')}/send", token) if "error" not in patched else patched
    if "error" in sent:
        return json.dumps({"success": False, "error": sent["error"]}, ensure_ascii=False)
    _consume_approval(fingerprint)
    return json.dumps(
        {"success": True, "reenviado_a": preview["para"], "cc": preview["cc"], "asunto": preview["asunto"]},
        ensure_ascii=False,
    )


def _direcciones_propias(token: str) -> set[str]:
    yo = _graph("GET", "/me?$select=mail,userPrincipalName", token)
    if not isinstance(yo, dict) or "error" in yo:
        return set()
    return {str(yo.get(k) or "").lower() for k in ("mail", "userPrincipalName") if yo.get(k)}


def _mensaje_propio_sin_destino(message: dict, token: str, reply_all: bool):
    remitente = ((message.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    if not remitente or remitente.lower() not in _direcciones_propias(token):
        return None
    destinatarios = [
        ((r or {}).get("emailAddress") or {}).get("address") or "" for r in (message.get("toRecipients") or [])
    ]
    if reply_all and any(destinatarios):
        return None
    return json.dumps(
        {
            "success": False,
            "error": (
                "Ese mensaje lo enviaste tú: responderlo iría a tu propia dirección, no a la otra persona. Responde al "
                "último mensaje del hilo que recibiste de otra persona (búscalo con ms365_mail_search y elige uno cuyo "
                f"remitente no seas tú) o, si quieres seguir el hilo con los mismos destinatarios ({', '.join(d for d in destinatarios if d) or 'los del correo original'}), "
                "llama con reply_all=true."
            ),
        },
        ensure_ascii=False,
    )


def _reply_email_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    message_id = _resolve_message_id(str(args.get("message_id") or "").strip())
    comment = str(args.get("comment") or "").strip()
    body = str(args.get("body") or "").strip()
    reply_all = bool(args.get("reply_all"))
    if not message_id or not (comment or body):
        return json.dumps(
            {"success": False, "error": "Se requiere message_id y comment o body (el texto de la respuesta)."}
        )
    rechazo = _id_rechazable(message_id)
    if rechazo:
        return json.dumps({"success": False, "error": rechazo}, ensure_ascii=False)
    attachments, attachment_error = _load_inline_images(args.get("inline_images"))
    if attachment_error:
        return json.dumps({"success": False, "error": attachment_error})
    firma = _nombre_firma(args)
    firma_imgs, firma_html, firma_error = _firma_config(firma)
    if firma_error:
        return json.dumps({"success": False, "error": firma_error}, ensure_ascii=False)
    ajena = _firma_de_otra_persona(token, firma, firma_html)
    if ajena:
        return json.dumps({"success": False, "error": ajena}, ensure_ascii=False)
    if firma_html and not body and comment:
        body = comment
    body = _quitar_despedida_repetida(_quitar_firma_pegada(body, firma_html), firma_html)
    _cuerpo_sin_firma = _texto_a_html(body) if body else html_escape(comment)
    body = _aplicar_firma_html(_texto_a_html(body), firma_imgs, firma_html)
    attachments = _merge_inline(firma_imgs, attachments)
    rotos = _cids_sin_imagen(body, attachments)
    if rotos:
        return json.dumps(
            {
                "success": False,
                "error": "El cuerpo trae imágenes cid: sin archivo ("
                + ", ".join(rotos)
                + "): es una firma pegada a mano. "
                "Quita la firma del cuerpo (termina en la despedida) y vuelve a llamar: el servidor pone la firma.",
            },
            ensure_ascii=False,
        )
    message, error = _message_for_action(token, message_id)
    if message is None:
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    propio = _mensaje_propio_sin_destino(message, token, reply_all)
    if propio:
        return propio
    if _aprobacion_central_activa():
        remitente = _direccion(message.get("from"))
        otros = [_direccion(r) for r in (message.get("toRecipients") or []) + (message.get("ccRecipients") or [])]
        if reply_all:
            yo = _graph("GET", "/me?$select=mail,userPrincipalName", token)
            propias = (
                {str(yo.get(k) or "").lower() for k in ("mail", "userPrincipalName")} if "error" not in yo else set()
            )
            otros = [o for o in otros if o.lower() not in propias]
        contenido = {
            "cuenta_emisora": f"m365:{user_id}",
            "mensaje_origen_id": message_id,
            "responder_a_todos": reply_all,
            "para": [remitente] if remitente else [],
            "cc": [o for o in otros if o and o.lower() != remitente.lower()] if reply_all else [],
            "asunto": re.sub(r"^(?!(re|rv)\s*:)", "RE: ", str(message.get("subject") or ""), flags=re.I),
            "cuerpo_html": body if body else html_escape(comment),
            "firma": firma or "",
            "firma_sha256": hashlib.sha256((firma_html or "").encode("utf-8")).hexdigest() if firma_html else "",
            "adjuntos": _contenido_central_correo(user_id, [], [], [], "", "", None, None, attachments)["adjuntos"],
        }
        return _enviar_central(args, contenido, attachments, "reply", TIPO_RESPONDER_CENTRAL)
    responde_a = ((message.get("from") or {}).get("emailAddress") or {}).get("address")
    tambien_a = (
        [((r or {}).get("emailAddress") or {}).get("address") for r in message.get("toRecipients") or []]
        if reply_all
        else []
    )
    if reply_all and responde_a and responde_a.lower() in _direcciones_propias(token) and tambien_a:
        responde_a, tambien_a = (
            ", ".join(a for a in tambien_a if a),
            [((r or {}).get("emailAddress") or {}).get("address") for r in message.get("ccRecipients") or []],
        )
    preview = {
        "accion": "responder a todos" if reply_all else "responder",
        "asunto": message.get("subject"),
        "responde_a": responde_a,
        "tambien_a": tambien_a,
        "texto": (_html_to_text(_cuerpo_sin_firma)[:500] if body else comment),
        "firma": firma or "sin firma",
        "borrador": _borrador_canonico(
            [
                ("Responder a", responde_a or ""),
                ("También a", ", ".join(a for a in tambien_a if a)),
                ("Asunto", re.sub(r"^(?!(re|rv)\s*:)", "RE: ", str(message.get("subject") or ""), flags=re.I)),
            ],
            _cuerpo_sin_firma,
            firma,
        ),
        "nota": "Reply sobre el hilo: los destinatarios salen del correo original, no se escriben a mano."
        + (" Incluye firma/imágenes en línea." if attachments else ""),
    }
    fingerprint = _action_fingerprint(
        user_id, "reply", message_id, reply_all, body, comment, _huella_adjuntos(attachments)
    )
    gate = _approval_gate(args, fingerprint, preview, "reply", token)
    if gate is not None:
        return gate
    if body or attachments:
        endpoint = "createReplyAll" if reply_all else "createReply"
        draft_payload = None if body else ({"comment": comment} if comment else None)
        draft = _graph("POST", f"/me/messages/{message_id}/{endpoint}", token, draft_payload)
        if "error" in draft:
            detail = str(draft["error"])
            if "403" in detail:
                detail += (
                    " Para responder con firma o imágenes falta el permiso Mail.ReadWrite: el usuario debe "
                    "reconectar su cuenta Microsoft 365."
                )
            return json.dumps({"success": False, "error": detail}, ensure_ascii=False)
        draft_id = draft.get("id")
        if body:
            actual = _graph("GET", f"/me/messages/{draft_id}?$select=body", token)
            previo = (actual.get("body") or {}).get("content") or "" if isinstance(actual, dict) else ""
            previo = _cita_en_espanol(previo, message.get("sentDateTime") or message.get("receivedDateTime"))
            patched = _graph(
                "PATCH", f"/me/messages/{draft_id}", token, {"body": {"contentType": "HTML", "content": body + previo}}
            )
            if "error" in patched:
                return json.dumps({"success": False, "error": patched["error"]}, ensure_ascii=False)
        for adjunto in attachments:
            att = _graph("POST", f"/me/messages/{draft_id}/attachments", token, adjunto)
            if "error" in att:
                return json.dumps({"success": False, "error": att["error"]}, ensure_ascii=False)
        sent = _graph("POST", f"/me/messages/{draft_id}/send", token)
    else:
        sent = _graph(
            "POST", f"/me/messages/{message_id}/{'replyAll' if reply_all else 'reply'}", token, {"comment": comment}
        )
    if "error" in sent:
        return json.dumps({"success": False, "error": sent["error"]}, ensure_ascii=False)
    _consume_approval(fingerprint)
    return json.dumps(
        {"success": True, "respondido": preview["accion"], "asunto": preview["asunto"]}, ensure_ascii=False
    )


def _mark_read_tool(args: dict, **_kwargs) -> str:
    if not _marcado_autorizado_tarea():
        return json.dumps({"success": False, "error": _MARCAR_NO_DECLARADO, "marcados": []}, ensure_ascii=False)
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, err = _token_herramienta(user_id)
    if not token:
        return _auth_error(err)
    raw_ids = args.get("message_ids")
    if raw_ids is None and args.get("message_id"):
        raw_ids = [args.get("message_id")]
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    ids = []
    for item in raw_ids or []:
        item = _resolve_message_id(str(item or "").strip())
        if item and item not in ids:
            ids.append(item)
    if not ids:
        return json.dumps({"success": False, "error": "Se requiere message_id o message_ids."})
    conocidos = _known_ids()
    no_reconocidos = [i for i in ids if i not in conocidos] if conocidos else []
    if conocidos:
        ids = [i for i in ids if i in conocidos]
    if not ids:
        return json.dumps(
            {
                "success": False,
                "error": "No marqué ningún correo: los ids indicados no aparecen en ninguna lista ni lectura reciente "
                "de este buzón. Primero lista o abre los correos (ms365_mail_list / ms365_mail_search / ms365_mail_get "
                "/ ms365_inbox_estado) y marca solo esos ids exactos; no marques ids de memoria.",
                "ids_no_reconocidos": no_reconocidos,
            },
            ensure_ascii=False,
        )
    marcar = args.get("is_read")
    is_read = True if marcar is None else bool(marcar)
    marcados = []
    errores = []
    for mid in ids:
        result = _graph("PATCH", f"/me/messages/{mid}", token, {"isRead": is_read})
        if "error" in result:
            errores.append({"message_id": mid, "error": _bad_id_message(result) or result["error"]})
        else:
            marcados.append(mid)
    estado = "leído" if is_read else "no leído"
    mensaje = f"{len(marcados)} correo(s) marcado(s) como {estado}"
    mensaje += f"; {len(errores)} con error." if errores else "."
    if no_reconocidos:
        mensaje += f" Ignoré {len(no_reconocidos)} id(s) no reconocido(s) que no venían de una lista/lectura."
    salida = {
        "success": len(errores) == 0,
        "marcados": marcados,
        "errores": errores,
        "isRead": is_read,
        "mensaje": mensaje,
    }
    if no_reconocidos:
        salida["ids_no_reconocidos"] = no_reconocidos
    return json.dumps(salida, ensure_ascii=False)


def _graph_get_bytes(path: str, token: str) -> tuple[bytes | None, str]:
    if token == BROKER_TOKEN:
        res = _broker_graph("GET", path)
        if "error" in res:
            return None, str(res["error"])
        data = base64.b64decode(res.get("_base64") or "")
        if len(data) > _MAX_DOWNLOAD_ATTACHMENT_BYTES:
            return None, f"Supera el máximo de {_MAX_DOWNLOAD_ATTACHMENT_BYTES // (1024 * 1024)} MB."
        return data, ""
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
    token, err = _token_herramienta(user_id)
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


_PROHIBIDOS_DRIVE = re.compile(r'[\x00-\x1f\x7f"*:<>?/\\|#%]')


def _safe_drive_name(name: str) -> str:
    nombre = _PROHIBIDOS_DRIVE.sub("_", unicodedata.normalize("NFC", str(name or ""))).strip(" .")
    if nombre.startswith("~$"):
        nombre = "_" + nombre[2:]
    return nombre[:120].rstrip(" .") or "archivo"


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


def _cids_de_firmas() -> set[str]:
    try:
        manifiesto = json.loads(_firma_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    cids = set()
    for entrada in manifiesto.values() if isinstance(manifiesto, dict) else ():
        if not isinstance(entrada, (list, dict)):
            continue
        items = entrada if isinstance(entrada, list) else (entrada.get("imagenes") or entrada.get("images") or [])
        cids.update(
            str(item.get("content_id")).strip() for item in items if isinstance(item, dict) and item.get("content_id")
        )
    return cids


def _send_email_tool(args: dict, **_kwargs) -> str:
    return _enviar_o_guardar(args)


def _create_draft_tool(args: dict, **_kwargs) -> str:
    return _enviar_o_guardar(args, solo_borrador=True)


def _guardar_borrador(token: str, message: dict, inline: list[dict], files: list[dict]) -> dict:
    draft = _graph("POST", "/me/messages", token, message)
    if "error" in draft:
        error = str(draft["error"])
        if "403" in error or "denied" in error.lower():
            return {"error": _NEEDS_MAIL_READWRITE}
        return draft
    message_id = draft.get("id")
    if not message_id:
        return {"error": "Microsoft no devolvió el borrador del correo."}
    for attachment in inline:
        result = _graph("POST", f"/me/messages/{message_id}/attachments", token, attachment)
        if "error" in result:
            _graph("DELETE", f"/me/messages/{message_id}", token)
            return result
    for entry in files:
        result = _attach_file_to_draft(token, message_id, entry)
        if "error" in result:
            _graph("DELETE", f"/me/messages/{message_id}", token)
            return result
    return {"id": message_id, "webLink": draft.get("webLink")}


_ASUNTO_DE_RESPUESTA = re.compile(r"^\s*(?:re|rv|respuesta)\s*:", re.IGNORECASE)


def _es_solo_para_mi(token: str, args: dict, to) -> bool:
    originales = _address_tokens(args.get("to"))
    if not originales or args.get("cc") or args.get("bcc"):
        return False
    return all(_YO.match(t) for t in originales) and all("@" in str(d) for d in _address_tokens(to))


_CUERPO_SIN_TEXTO = re.compile(
    r"^\W*(?:html|texto|text|plain|body|cuerpo|contenido|content|mensaje|markdown|md|\.{3}|…)\W*$", re.IGNORECASE
)


def _enviar_o_guardar(args: dict, solo_borrador: bool = False) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    central = _aprobacion_central_activa()
    if solo_borrador and central:
        return json.dumps(
            {"success": False, "error": "Guardar borradores no está disponible en este modo de aprobación central."},
            ensure_ascii=False,
        )
    token, err = _broker_token_check(user_id) if central else _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    to = _reemplazar_yo(args.get("to"), token)
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "").strip()
    if not to or not subject or not body:
        return json.dumps({"success": False, "error": "Se requieren: to, subject, body."})
    if _CUERPO_SIN_TEXTO.match(body):
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"El cuerpo del correo es solo «{body}»: eso es el nombre de un formato, no el texto. Escribe en "
                    "body el mensaje completo (saludo, párrafos y cierre); la firma la agrega el servidor. Si el usuario "
                    "de verdad quiere esa palabra como texto, pregúntaselo antes de insistir."
                ),
            },
            ensure_ascii=False,
        )
    if not solo_borrador and _ASUNTO_DE_RESPUESTA.match(subject) and not _es_solo_para_mi(token, args, to):
        return json.dumps(
            {
                "success": False,
                "error": (
                    "El asunto empieza con «Re:»: eso es una respuesta y ms365_mail_send la enviaría como un correo "
                    "NUEVO, fuera del hilo. Para responder usa ms365_mail_reply con el id del correo (búscalo con "
                    "ms365_mail_search; reply_all para conservar a los de copia). Si de verdad es un correo nuevo, "
                    "quita «Re:» del asunto."
                ),
            },
            ensure_ascii=False,
        )
    if to != args.get("to"):
        args = {**args, "to": to}
    for campo in ("cc", "bcc"):
        if args.get(campo):
            reemplazo = _reemplazar_yo(args.get(campo), token)
            if reemplazo != args.get(campo):
                args = {**args, campo: reemplazo}
    recipients, invalid = _parse_recipients(to)
    if invalid:
        return json.dumps(
            {"success": False, "error": f"Destinatarios sin dirección de correo válida: {', '.join(invalid)}."},
            ensure_ascii=False,
        )
    if not recipients:
        return json.dumps({"success": False, "error": "Se requiere al menos un destinatario válido en to."})
    cc_recipients, cc_invalid = _parse_recipients(args.get("cc"))
    if cc_invalid:
        return json.dumps(
            {"success": False, "error": f"Direcciones inválidas en cc: {', '.join(cc_invalid)}."},
            ensure_ascii=False,
        )
    bcc_recipients, bcc_invalid = _parse_recipients(args.get("bcc"))
    if bcc_invalid:
        return json.dumps(
            {"success": False, "error": f"Direcciones inválidas en bcc: {', '.join(bcc_invalid)}."},
            ensure_ascii=False,
        )
    if not args.get("firma") and any(f"cid:{cid}" in body for cid in _cids_de_firmas()):
        return json.dumps(
            {
                "success": False,
                "error": "La firma la agrega el servidor: no escribas las imágenes ni el bloque de firma a mano. "
                'Vuelve a llamar con firma="personal" y sin las referencias cid: de la firma, manteniendo el resto del correo.',
            },
            ensure_ascii=False,
        )
    attachments, attachment_error = _load_inline_images(args.get("inline_images"))
    if attachment_error:
        return json.dumps({"success": False, "error": attachment_error})
    firma = _nombre_firma(args)
    firma_imgs, firma_html, firma_error = _firma_config(firma)
    if firma_error:
        return json.dumps({"success": False, "error": firma_error}, ensure_ascii=False)
    ajena = _firma_de_otra_persona(token, firma, firma_html)
    if ajena:
        return json.dumps({"success": False, "error": ajena}, ensure_ascii=False)
    _cuerpo_sin_firma = _texto_a_html(_quitar_despedida_repetida(_quitar_firma_pegada(body, firma_html), firma_html))
    body = _aplicar_firma_html(_cuerpo_sin_firma, firma_imgs, firma_html)
    attachments = _merge_inline(firma_imgs, attachments)
    rotos = _cids_sin_imagen(body, attachments)
    if rotos:
        return json.dumps(
            {
                "success": False,
                "error": "El cuerpo trae imágenes cid: sin archivo ("
                + ", ".join(rotos)
                + "): es una firma pegada a mano. "
                "Quita la firma del cuerpo (termina en la despedida) y vuelve a llamar: el servidor pone la firma.",
            },
            ensure_ascii=False,
        )
    inline_bytes = sum(len(a["contentBytes"]) * 3 // 4 for a in attachments)
    files, file_summary, file_error = _load_file_attachments(args.get("attachments"), inline_bytes)
    if file_error:
        return json.dumps({"success": False, "error": file_error}, ensure_ascii=False)
    to_addrs = [r["emailAddress"]["address"] for r in recipients]
    cc_addrs = [r["emailAddress"]["address"] for r in cc_recipients]
    bcc_addrs = [r["emailAddress"]["address"] for r in bcc_recipients]
    de_ejemplo = _direcciones_de_ejemplo(to_addrs + cc_addrs + bcc_addrs)
    if de_ejemplo:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"{', '.join(de_ejemplo)} es una dirección de ejemplo, no real. No inventes direcciones: para el "
                    "propio usuario usa to=['yo']; para otra persona, ms365_find_person o pregúntale el correo."
                ),
            },
            ensure_ascii=False,
        )
    adjuntos = [e["name"] for e in file_summary] + [f"imagen:{a['contentId']}" for a in attachments]
    preview = {
        "accion": "enviar correo",
        "para": to_addrs,
        "cc": cc_addrs,
        "cco": bcc_addrs,
        "asunto": subject,
        "cuerpo": _html_to_text(_cuerpo_sin_firma)[:500],
        "adjuntos": adjuntos,
        "firma": firma or "sin firma",
        "borrador": _borrador_canonico(
            [
                ("Para", ", ".join(to_addrs)),
                ("CC", ", ".join(cc_addrs)),
                ("CCO", ", ".join(bcc_addrs)),
                ("Asunto", subject),
                ("Adjuntos", ", ".join(e["name"] for e in file_summary)),
            ],
            _cuerpo_sin_firma,
            firma,
        ),
    }
    if central:
        if inline_bytes + sum(entry["bytes"] for entry in file_summary) > _MAX_LARGE_ATTACHMENTS_TOTAL_BYTES:
            return json.dumps(
                {
                    "success": False,
                    "error": "Los adjuntos superan el máximo de 25 MB por correo. Compártelos con un "
                    "enlace de SharePoint/OneDrive, divídelos o envía menos archivos.",
                },
                ensure_ascii=False,
            )
        contenido = _contenido_central_correo(
            user_id, to_addrs, cc_addrs, bcc_addrs, subject, body, firma, firma_html, attachments + files
        )
        verif_central = (
            {} if args.get("aprobado_por_el_usuario") else _resolver_directorio(token, to_addrs + cc_addrs + bcc_addrs)
        )
        return _con_advertencia(
            _enviar_central(args, contenido, attachments + files, "send"), _advertencia_directorio(verif_central)
        )
    verif = {} if args.get("aprobado_por_el_usuario") else _resolver_directorio(token, to_addrs + cc_addrs + bcc_addrs)
    if verif:
        preview["verificacion_directorio"] = verif
        advertencia = _advertencia_directorio(verif)
        if advertencia and isinstance(preview.get("borrador"), str):
            preview["borrador"] = f"{preview['borrador']}\n\n{advertencia}"
    fingerprint = _action_fingerprint(
        user_id,
        "send",
        sorted(to_addrs),
        sorted(cc_addrs),
        sorted(bcc_addrs),
        subject,
        body,
        _huella_adjuntos(files),
        _huella_adjuntos(attachments),
    )
    if not solo_borrador:
        gate = _approval_gate(args, fingerprint, preview, "send", token)
        if gate is not None:
            return gate
    message = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": body},
        "toRecipients": recipients,
    }
    if cc_recipients:
        message["ccRecipients"] = cc_recipients
    if bcc_recipients:
        message["bccRecipients"] = bcc_recipients
    if solo_borrador:
        guardado = _guardar_borrador(token, message, attachments, file_summary)
        if "error" in guardado:
            return json.dumps({"success": False, "error": guardado["error"]}, ensure_ascii=False)
        return json.dumps(
            {
                "success": True,
                "estado": "borrador_guardado",
                "message": "Quedó guardado en la carpeta Borradores de Outlook. NO se envió.",
                "para": to_addrs,
                "cc": cc_addrs,
                "asunto": subject,
                "firma": firma or "sin firma",
                "adjuntos": [e["name"] for e in file_summary],
                "enlace": guardado.get("webLink"),
            },
            ensure_ascii=False,
        )
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
    _consume_approval(fingerprint)
    response = {
        "success": True,
        "estado_envio": "aceptada_proveedor",
        "message": "El servidor de correo aceptó el envío; todavía no está verificado en Enviados.",
        "recipients": len(recipients),
    }
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


TIPO_CORREO_CENTRAL = "correo.enviar"
TIPO_RESPONDER_CENTRAL = "correo.responder"
TIPO_REENVIAR_CENTRAL = "correo.reenviar"


def _direccion(r) -> str:
    return str(((r or {}).get("emailAddress") or {}).get("address") or "").strip()


def _aprobacion_central_activa() -> bool:
    return os.environ.get("HERMESHQ_APROBACION_CENTRAL", "").strip() == "1"


def _cabeceras_control() -> dict:
    return {
        "X-HermesHQ-Agent-ID": os.environ.get("HERMESHQ_AGENT_ID", ""),
        "X-HermesHQ-Agent-Token": os.environ.get("HERMESHQ_AGENT_TOKEN", ""),
        "X-HermesHQ-Attempt-ID": os.environ.get("HERMESHQ_ATTEMPT_ID", ""),
        "X-HermesHQ-Lease-Generation": os.environ.get("HERMESHQ_LEASE_GENERATION", ""),
        "Content-Type": "application/json",
    }


def _llamar_control(metodo: str, ruta: str, payload: dict | None = None, timeout: int = 30) -> dict:
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    if not base_url or not os.environ.get("HERMESHQ_AGENT_TOKEN"):
        return {"ok": False, "codigo": "sin_control", "error": "HermesHQ internal control no configurado"}
    datos = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{base_url}{ruta}", data=datos, method=metodo, headers=_cabeceras_control())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"_http": resp.status, **json.loads(resp.read().decode("utf-8") or "{}")}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detalle = json.loads(raw).get("detail", raw)
        except (json.JSONDecodeError, ValueError, AttributeError):
            detalle = raw
        if isinstance(detalle, dict):
            codigo = detalle.get("motivo") or detalle.get("codigo") or "error"
            return {
                "ok": False,
                "_http": exc.code,
                "codigo": codigo,
                "error": detalle.get("error") or codigo,
                **{k: v for k, v in detalle.items() if k not in ("motivo", "codigo", "error")},
            }
        return {"ok": False, "_http": exc.code, "codigo": "error", "error": str(detalle)[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "_http": 0, "codigo": "sin_respuesta", "error": f"{type(exc).__name__}: {exc}"}


def _sha256_b64(contenido_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(contenido_b64 or "")).hexdigest()


def _contenido_central_correo(user_id, para, cc, cco, asunto, cuerpo_html, firma, firma_html, adjuntos) -> dict:
    return {
        "cuenta_emisora": f"m365:{user_id}",
        "para": list(para),
        "cc": list(cc),
        "cco": list(cco),
        "asunto": asunto,
        "cuerpo_html": cuerpo_html,
        "firma": firma or "",
        "firma_sha256": hashlib.sha256((firma_html or "").encode("utf-8")).hexdigest() if firma_html else "",
        "adjuntos": [
            {
                "nombre": str(a.get("name") or ""),
                "content_id": str(a.get("contentId") or ""),
                "sha256": _sha256_b64(a.get("contentBytes") or ""),
                "bytes": len(base64.b64decode(a.get("contentBytes") or "")),
            }
            for a in adjuntos
        ],
    }


def _adjuntos_para_broker(adjuntos) -> list[dict]:
    return [
        {
            "nombre": str(a.get("name") or ""),
            "content_id": str(a.get("contentId") or ""),
            "content_type": str(a.get("contentType") or "application/octet-stream"),
            "contenido_b64": a.get("contentBytes") or "",
        }
        for a in adjuntos
    ]


def _pedir_aprobacion_central(
    args: dict, contenido: dict, herramienta: str, aviso: str | None = None, tipo: str = TIPO_CORREO_CENTRAL
) -> str:
    parametros = {
        "herramienta": herramienta,
        "args": {k: v for k, v in args.items() if k not in ("aprobado_por_el_usuario", "aprobacion_id")},
    }
    creada = _llamar_control(
        "POST",
        "/control/aprobaciones",
        {"task_id": _current_task_id(), "tipo": tipo, "contenido": contenido, "parametros": parametros},
    )
    if not creada.get("ok"):
        return json.dumps(
            {"success": False, "ejecutado": False, "codigo": creada.get("codigo"), "error": creada.get("error")},
            ensure_ascii=False,
        )
    if creada.get("estado") == "aprobada" and creada.get("id"):
        return json.dumps({"_aprobada_existente": creada["id"]})
    if creada.get("estado") == "ejecutada" and creada.get("id"):
        return json.dumps(
            {
                "success": True,
                "ya_enviado": True,
                "aprobacion_id": creada["id"],
                "estado_envio": creada.get("envio_estado"),
                "entregado": False,
                "mensaje": "Este correo ya se envió con esa aprobación: no se repitió.",
            },
            ensure_ascii=False,
        )
    if creada.get("estado") == "ejecutando" and creada.get("id"):
        return json.dumps(
            {
                "success": False,
                "estado": "desconocido",
                "aprobacion_id": creada["id"],
                "error": "Este correo está en curso o pendiente de conciliación. No lo repito.",
            },
            ensure_ascii=False,
        )
    resultado = {
        "success": False,
        "requiere_aprobacion": True,
        "aprobacion_id": creada.get("id"),
        "borrador": creada.get("vista_previa"),
        "siguiente": "Copia el campo 'borrador' TAL CUAL en tu respuesta (es lo que se enviará, incluida la firma) y "
        "pregunta si lo envía. Solo el usuario puede aprobar: con el botón o respondiendo «sí». Nunca digas que se "
        "envió sin success=true.",
    }
    if creada.get("tarjeta"):
        resultado["tarjeta_aprobacion"] = creada["tarjeta"]
    if aviso:
        resultado["error"] = aviso
    return json.dumps(resultado, ensure_ascii=False)


def _enviar_central(
    args: dict, contenido: dict, adjuntos: list[dict], herramienta: str, tipo: str = TIPO_CORREO_CENTRAL
) -> str:
    aprobacion_id = str(args.get("aprobacion_id") or "").strip()
    if not aprobacion_id:
        pedido = _pedir_aprobacion_central(args, contenido, herramienta, tipo=tipo)
        existente = json.loads(pedido).get("_aprobada_existente")
        if not existente:
            return pedido
        aprobacion_id = existente
        args = dict(args, aprobacion_id=existente)
    respuesta = _llamar_control(
        "POST",
        "/control/m365/correo/enviar",
        {
            "tarea_id": _current_task_id(),
            "aprobacion_id": aprobacion_id,
            "contenido": contenido,
            "adjuntos": _adjuntos_para_broker(adjuntos),
        },
        timeout=600,
    )
    if respuesta.get("ok"):
        return json.dumps(
            {
                "success": True,
                "estado_envio": respuesta.get("envio_estado"),
                "entregado": False,
                "aprobacion_id": aprobacion_id,
                "message": respuesta.get("mensaje")
                or "El servidor de correo aceptó el envío; todavía no está verificado en Enviados.",
            },
            ensure_ascii=False,
        )
    codigo = respuesta.get("codigo")
    if codigo in ("huella_distinta", "pendiente", "expirada", "rechazada", "fallida"):
        avisos = {
            "huella_distinta": "No se envió: el correo cambió respecto de lo aprobado. Se pidió una aprobación nueva.",
            "pendiente": "No se envió: el usuario todavía no aprueba este correo.",
            "expirada": "No se envió: la aprobación expiró. Se pidió una nueva.",
            "rechazada": "No se envió: el usuario rechazó este correo.",
            "fallida": "No se envió: el intento anterior falló. Se pidió una aprobación nueva.",
        }
        if codigo in ("rechazada", "pendiente"):
            return json.dumps(
                {"success": False, "ejecutado": False, "codigo": codigo, "error": avisos[codigo]}, ensure_ascii=False
            )
        pedido = _pedir_aprobacion_central(dict(args, aprobacion_id=None), contenido, herramienta, avisos[codigo], tipo)
        if json.loads(pedido).get("_aprobada_existente"):
            return _enviar_central(dict(args, aprobacion_id=None), contenido, adjuntos, herramienta, tipo)
        return pedido
    if codigo == "graph_rechazo" and ":403" in str(respuesta.get("error") or ""):
        return json.dumps(
            {"success": False, "ejecutado": False, "codigo": "requiere_mail_readwrite", "error": _NEEDS_MAIL_READWRITE},
            ensure_ascii=False,
        )
    incierto = codigo in ("resultado_incierto", "sin_respuesta", "conciliacion_requerida")
    salida = {
        "success": False,
        "ejecutado": False if not incierto else None,
        "codigo": codigo,
        "error": respuesta.get("error") or codigo,
    }
    if incierto:
        salida["estado_envio"] = "desconocida"
        salida["error"] = (
            "No sé si el correo salió. No lo reenvío: primero hay que conciliarlo (revisar Enviados). "
            f"Detalle: {respuesta.get('error') or codigo}"
        )
    if codigo == "ya_ejecutada" or codigo == "accion_ya_ejecutada":
        salida["ya_enviado"] = True
        salida["error"] = "Este correo ya se envió (o está en curso) con esa aprobación: no lo reenvié."
    return json.dumps(salida, ensure_ascii=False)


def ejecutar_aprobadas_central() -> dict | None:
    if not _aprobacion_central_activa() or _modo_automatico() or not es_aprobacion_pura():
        return None
    listas = _llamar_control("GET", f"/control/aprobaciones/aprobadas?task_id={urllib.parse.quote(_current_task_id())}")
    aprobadas = [
        a
        for a in ((listas.get("aprobadas") or []) if listas.get("ok") else [])
        if a.get("tipo_accion") in (TIPO_CORREO_CENTRAL, TIPO_RESPONDER_CENTRAL, TIPO_REENVIAR_CENTRAL)
    ]
    if not aprobadas:
        return None
    ultima = aprobadas[-1]
    parametros = ultima.get("parametros") or {}
    herramientas = {
        "send": ("ms365_mail_send", _send_email_tool),
        "reply": ("ms365_mail_reply", _reply_email_tool),
        "forward": ("ms365_mail_forward", _forward_email_tool),
    }
    if parametros.get("herramienta") not in herramientas:
        return None
    nombre, funcion = herramientas[parametros["herramienta"]]
    args = dict(parametros.get("args") or {}, aprobacion_id=ultima["id"])
    try:
        resultado = json.loads(funcion(args))
    except Exception as exc:  # noqa: BLE001
        resultado = {"success": False, "error": f"Falló el envío: {exc}"}
    contenido = ultima.get("contenido") or {}
    preview = {
        "para": contenido.get("para"),
        "cc": contenido.get("cc"),
        "cco": contenido.get("cco"),
        "asunto": contenido.get("asunto"),
    }
    return {"herramienta": nombre, "args": args, "resultado": resultado, "preview": preview}


def _kql_value(value) -> str:
    return str(value or "").replace('"', " ").strip()


_KQL_FIELD_TOKEN = re.compile(r'[^\s"()\\,;:]+')


def _kql_tokens(value) -> list[str]:
    return _KQL_FIELD_TOKEN.findall(_kql_value(value))


def _kql_field_parts(name: str, value) -> list[str]:
    return [f"{name}:{token}" for token in _kql_tokens(value)]


def _kql_date(value, label) -> tuple[str | None, str | None]:
    parsed, error = _parse_since(value)
    if error:
        return None, error.replace("since", label)
    return (parsed[:10] if parsed else None), None


_KQL_EMBEDDED_FIELD = re.compile(r'\b(from|subject|to|cc)\s*:\s*(?:"([^"]*)"|(\S+))', re.IGNORECASE)


def _split_embedded_fields(query: str) -> tuple[str, dict[str, str]]:
    fields: dict[str, str] = {}

    def take(match):
        name = match.group(1).lower()
        value = _kql_value(match.group(2) if match.group(2) is not None else match.group(3))
        if value and name not in fields:
            fields[name] = value
        return " "

    rest = _KQL_EMBEDDED_FIELD.sub(take, query)
    rest = re.sub(r"\b(from|subject|to|cc)\s*:", " ", rest, flags=re.IGNORECASE)
    return " ".join(_kql_value(rest).split()), fields


def _kql_phrase(value) -> str:
    texto = re.sub(r"[\x00-\x1f\\\",;]", " ", str(value or ""))
    return " ".join(texto.split())[:100]


_PALABRA_SIMPLE = re.compile(r"^[\wáéíóúñüÁÉÍÓÚÑÜ.\-@]{2,50}$")
_CAMPO_KQL = re.compile(r"^(?:(?:from|subject|to|cc):\S+|received(?:>=|<=)\S+|hasattachments:true)$", re.I)


def _texto_libre_kql(query: str) -> str:
    if '"' in query or "\\" in query:
        return query
    terminos = []
    for termino in query.split():
        if _PALABRA_SIMPLE.match(termino) or _CAMPO_KQL.match(termino):
            terminos.append(termino)
        else:
            terminos.append(f'"{termino}"')
    return " ".join(terminos)


def _build_search_kql(args: dict) -> tuple[str | None, str | None]:
    parts = []
    query = str(args.get("query") or "").strip()
    if query:
        parts.append(_texto_libre_kql(query))
    for name, key in (("from", "from"), ("subject", "subject"), ("to", "to"), ("cc", "cc")):
        phrase = _kql_phrase(args.get(key))
        if phrase:
            parts.append(f'{name}:"{phrase}"')
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
    token, err = _broker_token_check(user_id)
    if not token:
        return _auth_error(err)
    kql, kql_error = _build_search_kql(args)
    if kql_error:
        return json.dumps({"success": False, "error": kql_error}, ensure_ascii=False)
    count = max(1, min(int(args.get("count") or 10), 50))
    preview_chars = _int_arg(args.get("preview_chars"), _DEFAULT_PREVIEW_CHARS, 0, 255)
    raw_folder = str(args.get("folder") or "").strip()
    folder = None
    if raw_folder:
        folder, folder_error = _resolver_carpeta(raw_folder)
        if folder_error:
            return json.dumps({"success": False, "error": folder_error}, ensure_ascii=False)
    literal = urllib.parse.quote('"' + kql + '"', safe="")
    cursor = str(args.get("cursor") or "").strip()
    cursor_part = f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else ""
    base = f"/me/mailFolders/{folder}/messages" if folder else "/me/messages"
    path = f"{base}?$search={literal}&$top={count}{cursor_part}"
    result = _broker_graph("GET", path)
    if not folder and "carpeta_de_correo_fuera_del_alcance" in str(result.get("error") or ""):
        result = _broker_graph("GET", f"/me/mailFolders/inbox/messages?$search={literal}&$top={count}{cursor_part}")
    if "error" in result:
        error = str(result["error"])
        if any(
            m in error
            for m in ("consulta_no_valida", "termino_no_permitido", "operador_no_permitido", "valor_no_permitido")
        ):
            error += " Usa solo from:, subject:, received>= o hasattachments:true."
        elif "busqueda_no_disponible" in error:
            error += " Simplifica la búsqueda: usa menos palabras o los campos from, subject y since."
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    messages = list(result.get("items") or [])
    _cap_previews(messages, preview_chars)
    _con_hora_local(messages)
    _remember_ids(messages)
    sin_id = _anotar_ids_cortos(messages)
    answer = {
        "success": True,
        "count": len(messages),
        "search": kql,
        "ahora_chile": _ahora_chile(),
        "messages": messages,
    }
    if sin_id:
        answer["aviso_ids"] = _AVISO_IDS_CORTOS
    if result.get("siguiente_cursor"):
        answer["cursor_siguiente"] = result["siguiente_cursor"]
    return json.dumps(answer, ensure_ascii=False)


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
    user_id = _task_user_id()
    if not user_id:
        return None, None
    token, _ = _token_herramienta(user_id)
    if not token:
        return None, None
    resultado = _graph(
        "GET",
        "/me/mailFolders/inbox/messages?$top=100&$select=id,conversationId,receivedDateTime&$filter=isRead%20eq%20false",
        token,
    )
    if "error" in resultado:
        return None, None
    mensajes = [m for m in resultado.get("value", []) if isinstance(m, dict) and m.get("id")]
    if len(mensajes) >= 100:
        return None, None
    no_leidos = {m["id"] for m in mensajes}
    recibidos = {
        m["id"]: (m["conversationId"], _fecha_graph(m.get("receivedDateTime")))
        for m in mensajes
        if m.get("conversationId")
    }
    respondidos: set = set()
    if recibidos:
        enviados = _graph(
            "GET",
            "/me/mailFolders/sentitems/messages?$top=100&$select=conversationId,sentDateTime&$orderby=sentDateTime%20desc",
            token,
        )
        if "error" not in enviados:
            ultimo_envio: dict = {}
            for m in enviados.get("value", []):
                if not isinstance(m, dict) or not m.get("conversationId"):
                    continue
                enviado = _fecha_graph(m.get("sentDateTime"))
                if enviado and (m["conversationId"] not in ultimo_envio or enviado > ultimo_envio[m["conversationId"]]):
                    ultimo_envio[m["conversationId"]] = enviado
            respondidos = {
                mid
                for mid, (conv, recibido) in recibidos.items()
                if conv in ultimo_envio and recibido and ultimo_envio[conv] > recibido
            }
    return no_leidos, respondidos


def _fecha_graph(valor):
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


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
        "siguiente": "Llama ms365_mail_list(folder=inbox, since=<since>, count=<count>, preview_chars=160, unread_only=true), clasifica los nuevos y luego ms365_inbox_estado(nuevos=[...]). Los correos ya leídos o respondidos no son pendientes.",
    }


def _inbox_estado_tool(args, **_kwargs):
    if args.get("accion") == "ventana":
        try:
            return json.dumps(_estado_ventana(_estado_fecha(args.get("ahora"), datetime.now(UTC))), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001  # report instead of crashing the run
            return json.dumps({"success": False, "error": f"No se pudo calcular la ventana: {exc}"}, ensure_ascii=False)
    try:
        ahora = _estado_fecha(args.get("ahora"), datetime.now(UTC))
        no_leidos, respondidos = _estado_no_leidos() if args.get("verificar_leidos", True) else (None, None)
        with _bloqueo_archivo(_estado_path()):
            return _inbox_estado_actualizar(args, ahora, no_leidos, respondidos)
    except Exception as exc:  # noqa: BLE001  # state errors must be reported, not crash the run
        return json.dumps({"success": False, "error": f"No se pudo actualizar el estado: {exc}"}, ensure_ascii=False)


def _inbox_estado_actualizar(args, ahora, no_leidos, respondidos):
    datos, pendientes = _estado_cargar(ahora)
    previo = datos.get("backlog_antiguo") if isinstance(datos.get("backlog_antiguo"), dict) else {}
    resueltos = {_resolve_message_id(str(x).strip()) for x in args.get("resueltos") or [] if str(x).strip()}
    if no_leidos is not None:
        resueltos |= {p["id"] for p in pendientes if len(p["id"]) >= 60 and p["id"] not in no_leidos}
        resueltos |= respondidos or set()
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
    _cu_escribir_atomico(_estado_path(), texto)
    salida["archivo_kb"] = round(len(texto.encode("utf-8")) / 1024, 1)
    if salida["archivo_kb"] > _ESTADO_MAX_KB:
        salida["aviso"] = f"El estado pesa más de {_ESTADO_MAX_KB} KB; revisa los pendientes."
    salida["siguiente"] = (
        "Arma el informe con estos datos (no abras ni modifiques work/cron_inbox_estado.json, ya quedó actualizado)."
    )
    _remember_ids([{"id": p["id"]} for p in quedan if isinstance(p, dict) and p.get("id")])
    return json.dumps(salida, ensure_ascii=False)


_CU_FILE = "correo_unificado_estado.json"
_CU_SCRATCH_LEGADO = ".correo_sync_plan.json"
_CU_PLANES = ".correo_sync_planes"
_CU_PLAN_HUERFANO_HORAS = 24
_CU_MAX_KB = 80
_CU_ESCALA_HORAS = 2
_CU_PURGA_DIAS = 7
_CU_TOP_FILAS = 40
_CU_BAKS = 3
_CU_SIN_MARCA_HORAS = 48
_CU_VENTANA_MAX_HORAS = 168
_CU_SOLAPAMIENTO_MIN = 10
_CU_ENTREGADOS_DIAS = 14
_CU_PAGINA = 50
_CU_PAGINAS_MAX = 10
_CU_POR_CLASIFICAR_MAX = 400
_CU_SECCIONES_TOPE = ("pending", "resolved", "discarded", "history")

_CU_TEMP = {"INMEDIATO": 5, "CRÍTICO": 4, "CRITICO": 4, "CALIENTE": 3, "TIBIO": 2, "FRÍO": 1, "FRIO": 1}
_CU_OPS = {
    "PENDIENTE_DE_MÍ": 7,
    "PENDIENTE_DE_MI": 7,
    "PENDIENTE": 6,
    "ESPERANDO_TERCERO": 5,
    "EN_SEGUIMIENTO": 4,
    "EN_COPIA": 3,
    "GESTIONADO_POR_TICKET": 2,
    "REUNION_REALIZADA": 1,
    "SIN_ACCIÓN": 0,
    "SIN_ACCION": 0,
}
_CU_DUE = {"VENCIDO": 4, "HOY": 3, "PRÓXIMO": 2, "PROXIMO": 2, "ESTA_SEMANA": 1, "SIN_PLAZO": 0}
_CU_ESCALABLES = ("PENDIENTE_DE_MÍ", "PENDIENTE_DE_MI", "PENDIENTE")
_CU_LADDER = ("TIBIO", "CALIENTE", "CRÍTICO")
_CU_PRINCIPALES = ("INMEDIATO", "CRÍTICO", "CALIENTE", "TIBIO")
_CU_CAT_NORM = {
    "CRITICO": "CRÍTICO",
    "FRIO": "FRÍO",
    "CRÍTICO": "CRÍTICO",
    "FRÍO": "FRÍO",
    "INMEDIATO": "INMEDIATO",
    "CALIENTE": "CALIENTE",
    "TIBIO": "TIBIO",
}
_CU_OPS_NORM = {"SIN_ACCION": "SIN_ACCIÓN", "PENDIENTE_DE_MI": "PENDIENTE_DE_MÍ"}
_CU_CAMPOS = (
    "display_id",
    "message_id",
    "internet_message_id",
    "subject",
    "sender_name",
    "sender_address",
    "relationship",
    "display_category",
    "base_temperature",
    "operational_status",
    "action_type",
    "action_required",
    "requires_response",
    "due_status",
    "due_date",
    "first_detected",
    "last_reviewed",
)
_CU_AUTO_RE = re.compile(
    r"(no[-_]?reply|noreply|do[-_]?not[-_]?reply|notifications?@|mailer-daemon|newsletter|digest|"
    r"quarantine@|postmaster@|automated|notificaciones?@|alerta?s?@|no-responder|@.*mailchimp)",
    re.I,
)


def _cu_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / _CU_FILE


def _cu_bloqueo():
    return _bloqueo_archivo(_cu_path())


def _cu_plan_path(task_id: str | None = None) -> Path:
    tid = _current_task_id() if task_id is None else task_id
    seguro = re.sub(r"[^A-Za-z0-9_.-]", "_", tid)[:40] or "sin_tarea"
    huella = hashlib.sha256(tid.encode("utf-8")).hexdigest()[:10]
    return _cu_path().parent / _CU_PLANES / f"{seguro}-{huella}.json"


def _cu_limpiar_planes(propio: Path) -> None:
    limite = time.time() - _CU_PLAN_HUERFANO_HORAS * 3600
    legado = _cu_path().parent / _CU_SCRATCH_LEGADO
    for ruta in [legado] + (list(propio.parent.glob("*.json")) if propio.parent.is_dir() else []):
        try:
            if ruta != propio and ruta.is_file() and (ruta == legado or ruta.stat().st_mtime < limite):
                ruta.unlink()
        except OSError:
            pass


def _cu_cat(v) -> str:
    return _CU_CAT_NORM.get(str(v or "").strip().upper(), str(v or "").strip() or "TIBIO")


def _cu_ops_norm(v) -> str:
    t = str(v or "").strip()
    return _CU_OPS_NORM.get(t, t) or "SIN_ACCIÓN"


def _cu_norm_subject(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip()).lower()


def _cu_rr_true(it) -> bool:
    return it.get("requires_response") in (True, "true", "True", 1)


def _cu_parse_due(v):
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(v or ""))
    return m.group() if m else None


def _cu_has_plazo(it) -> bool:
    return bool(_cu_parse_due(it.get("due_date"))) or str(it.get("due_status")) not in ("SIN_PLAZO", "None", "", "null")


def _cu_display_num(it) -> int:
    m = re.search(r"\d+", str(it.get("display_id") or ""))
    return int(m.group()) if m else 10**9


def _cu_pick_id(it) -> str:
    for k in ("message_id", "email_id", "id"):
        v = it.get(k)
        if isinstance(v, str) and len(v) > 40:
            return v
    v = it.get("message_id") or it.get("email_id") or it.get("id")
    return str(v) if isinstance(v, str) and len(v) > 40 else ""


def _cu_key(it):
    mid = _cu_pick_id(it)
    subj = _cu_norm_subject(it.get("subject"))
    return (mid if mid else f"noid:{id(it)}", subj)


def _cu_remitente_auto(it) -> bool:
    hay = f"{it.get('sender_name') or ''} {it.get('sender_address') or ''} {it.get('sender') or ''}"
    return bool(_CU_AUTO_RE.search(hay))


def _cu_es_frio_inerte(it) -> bool:
    return (
        _cu_cat(it.get("display_category")) == "FRÍO"
        and _cu_ops_norm(it.get("operational_status")) == "SIN_ACCIÓN"
        and not _cu_rr_true(it)
        and not _cu_has_plazo(it)
    )


def _cu_merge_group(items):
    if len(items) == 1:
        return dict(items[0])
    hottest = max(items, key=lambda it: _CU_TEMP.get(str(it.get("display_category")), 0))
    actionable = max(items, key=lambda it: _CU_OPS.get(str(it.get("operational_status")), 0))
    base = dict(actionable)
    base["display_id"] = min(items, key=_cu_display_num).get("display_id")
    base["display_category"] = hottest.get("display_category")
    base["base_temperature"] = hottest.get("base_temperature") or hottest.get("display_category")
    base["operational_status"] = actionable.get("operational_status")
    base["first_detected"] = min(
        (str(i.get("first_detected")) for i in items if i.get("first_detected")), default=base.get("first_detected")
    )
    base["last_reviewed"] = max(
        (str(i.get("last_reviewed")) for i in items if i.get("last_reviewed")), default=base.get("last_reviewed")
    )
    req = [i for i in items if _cu_rr_true(i)]
    base["requires_response"] = True if req else _cu_rr_true(base)
    cand = [i for i in req if str(i.get("action_required") or "").strip()] or [
        i for i in items if str(i.get("action_required") or "").strip()
    ]
    if cand:
        src = max(cand, key=lambda i: _CU_TEMP.get(str(i.get("display_category")), 0))
        base["action_required"] = src.get("action_required")
        base["action_type"] = src.get("action_type")
    con_fecha = [i for i in items if _cu_parse_due(i.get("due_date"))]
    if con_fecha:
        src = min(con_fecha, key=lambda i: _cu_parse_due(i.get("due_date")))
        base["due_date"] = src.get("due_date")
        base["due_status"] = src.get("due_status")
    else:
        con_st = [i for i in items if str(i.get("due_status")) not in ("SIN_PLAZO", "None", "", "null")]
        if con_st:
            src = max(con_st, key=lambda i: _CU_DUE.get(str(i.get("due_status")), 0))
            base["due_status"] = src.get("due_status")
            base["due_date"] = src.get("due_date")
    return base


def _cu_dedup(pend):
    groups, order = {}, []
    for it in pend:
        k = _cu_key(it)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(it)
    merged = {k: _cu_merge_group(groups[k]) for k in order}
    degradados = []
    for k in order:
        copias = groups[k]
        if len(copias) < 2:
            continue
        m = merged[k]
        did = str(m.get("display_id"))
        if any(_cu_rr_true(c) for c in copias) and not _cu_rr_true(m):
            degradados.append((did, "requires_response"))
        if _CU_TEMP.get(str(m.get("display_category")), 0) < max(
            _CU_TEMP.get(str(c.get("display_category")), 0) for c in copias
        ):
            degradados.append((did, "categoria"))
        if _CU_OPS.get(str(m.get("operational_status")), 0) < max(
            _CU_OPS.get(str(c.get("operational_status")), 0) for c in copias
        ):
            degradados.append((did, "estado"))
        fechas = [_cu_parse_due(c.get("due_date")) for c in copias if _cu_parse_due(c.get("due_date"))]
        if fechas:
            mf = _cu_parse_due(m.get("due_date"))
            if not mf or mf > min(fechas):
                degradados.append((did, "plazo"))
    return [merged[k] for k in order], degradados


def _cu_horas(first_detected, ahora) -> float:
    return max(0.0, (ahora - _estado_fecha(first_detected, ahora)).total_seconds() / 3600)


def _cu_display_category(item, ahora) -> str:
    base = _cu_cat(item.get("base_temperature") or item.get("display_category"))
    dc = _cu_cat(item.get("display_category"))
    if _CU_TEMP.get(base, 0) < _CU_TEMP.get(dc, 0):
        base = dc
    if base not in ("TIBIO", "CALIENTE"):
        return base
    ops = _cu_ops_norm(item.get("operational_status"))
    if ops not in _CU_ESCALABLES or not _cu_rr_true(item):
        return base
    pasos = int(_cu_horas(item.get("first_detected"), ahora) // _CU_ESCALA_HORAS)
    i = _CU_LADDER.index(base)
    return _CU_LADDER[min(i + pasos, len(_CU_LADDER) - 1)]


def _cu_bak(path: Path, n: int) -> Path:
    return path.parent / f"{path.name}.bak-{n}"


def _cu_leer_json(path: Path):
    try:
        datos = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return datos if isinstance(datos, dict) else None


def _cu_load():
    path = _cu_path()
    datos = {}
    for candidato in [path] + [_cu_bak(path, n) for n in range(1, _CU_BAKS + 1)]:
        leido = _cu_leer_json(candidato) if candidato.is_file() else None
        if leido is not None:
            datos = leido
            break
    datos.setdefault("version", 5)
    if not isinstance(datos.get("revision"), int):
        datos["revision"] = 0
    if not isinstance(datos.get("marcas"), dict):
        datos["marcas"] = {}
    if not isinstance(datos.get("por_clasificar"), list):
        datos["por_clasificar"] = []
    if not isinstance(datos.get("entregados"), dict):
        datos["entregados"] = {}
        for sec in ("resolved", "discarded"):
            for it in datos.get(sec) or []:
                if isinstance(it, dict) and it.get("message_id"):
                    fecha = _cu_fecha_item(it.get("resolved_at") or it.get("discarded_at") or it.get("last_reviewed"))
                    for h in _cu_huellas({"id": it.get("message_id")}):
                        datos["entregados"][h] = fecha or datetime.now(UTC).strftime("%Y-%m-%d")
    for k in ("pending", "resolved", "discarded", "ticket_managed", "history"):
        if not isinstance(datos.get(k), list):
            datos[k] = []
    if not isinstance(datos.get("frios_consolidados"), dict):
        datos["frios_consolidados"] = {"count": 0, "updated_at": None}
    cands = [
        _cu_display_num(p)
        for sec in ("pending", "resolved", "discarded", "history")
        for p in datos.get(sec, [])
        if isinstance(p, dict) and _cu_display_num(p) < 10**9
    ]
    prev = datos.get("next_display_id") if isinstance(datos.get("next_display_id"), int) else 0
    datos["next_display_id"] = max(cands + [prev - 1, 0]) + 1
    datos["current_results"] = []
    return datos


def _cu_escribir_atomico(path: Path, texto: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporal = path.parent / f"{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}"
    try:
        with open(temporal, "w", encoding="utf-8") as f:
            f.write(texto)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporal, path)
    finally:
        if temporal.exists():
            temporal.unlink()


def _cu_escribir(datos) -> float:
    with _cu_bloqueo():
        datos["current_results"] = []
        datos["revision"] = (datos.get("revision") if isinstance(datos.get("revision"), int) else 0) + 1
        texto = json.dumps(datos, ensure_ascii=False, separators=(",", ":"))
        path = _cu_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and _cu_leer_json(path) is not None:
            for n in range(_CU_BAKS, 1, -1):
                if _cu_bak(path, n - 1).is_file():
                    os.replace(_cu_bak(path, n - 1), _cu_bak(path, n))
            _cu_escribir_atomico(_cu_bak(path, 1), path.read_text(encoding="utf-8"))
        _cu_escribir_atomico(path, texto)
        return round(len(texto.encode("utf-8")) / 1024, 1)


def _cu_fecha_item(v):
    return _cu_parse_due(v) or str(v or "")[:10]


def _cu_purgar(datos, ahora):
    limite = (ahora - timedelta(days=_CU_PURGA_DIAS)).strftime("%Y-%m-%d")
    for sec in ("resolved", "discarded", "history"):
        nuevos = []
        for it in datos.get(sec, []):
            f = _cu_fecha_item(
                it.get("resolved_at")
                or it.get("discarded_at")
                or it.get("date")
                or it.get("last_reviewed")
                or it.get("first_detected")
            )
            if not f or f >= limite:
                nuevos.append(it)
        datos[sec] = nuevos


def _cu_edad(item, ahora) -> str:
    minutos = int(round(_cu_horas(item.get("first_detected"), ahora) * 60))
    if minutos < 60:
        return f"{minutos} min"
    horas, resto = divmod(minutos, 60)
    if horas < 24:
        return f"{horas} h {resto} min" if resto else f"{horas} h"
    dias, horas = divmod(horas, 24)
    return f"{dias} d {horas} h" if horas else f"{dias} d"


def _cu_sin_tildes(texto) -> str:
    return unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii").lower().strip()


def _cu_relacion_fila(item) -> str:
    r = _cu_sin_tildes(item.get("relationship"))
    if r.startswith("no dirigido"):
        return "No dirigido"
    if "copia" in r or r in ("cc", "en_copia"):
        return "En copia"
    if r.startswith("para"):
        return "Para mí"
    return "—"


def _cu_plazo_fila(item, ahora) -> str:
    from zoneinfo import ZoneInfo

    hoy = ahora.astimezone(ZoneInfo("America/Santiago")).date()
    fecha = _cu_parse_due(item.get("due_date")) or _cu_parse_due(item.get("due_status"))
    if fecha:
        try:
            d = datetime.strptime(fecha, "%Y-%m-%d").date()
        except ValueError:
            d = None
        if d:
            if d <= hoy:
                return "Hoy"
            if d == hoy + timedelta(days=1):
                return "Mañana"
            return d.strftime("%d-%m")
    s = _cu_sin_tildes(item.get("due_status"))
    if any(x in s for x in ("hoy", "urgente", "inmediat", "durante el dia")):
        return "Hoy"
    if "manana" in s:
        return "Mañana"
    return "Sin plazo"


def _cu_fila(item, ahora):
    return {
        "id": item.get("display_id"),
        "edad": _cu_edad(item, ahora),
        "correo": f"{item.get('subject') or ''} — {item.get('sender_name') or ''}".strip(" —"),
        "asunto": item.get("subject") or "",
        "remitente": item.get("sender_name") or "",
        "relacion": _cu_relacion_fila(item),
        "accion": item.get("action_required") or "",
        "estado": _cu_ops_norm(item.get("operational_status")),
        "plazo": _cu_plazo_fila(item, ahora),
        "respuesta": "Sí" if _cu_rr_true(item) else "No",
        "categoria": item.get("display_category"),
    }


def _cu_totales(pend):
    t = {"inmediato": 0, "critico": 0, "caliente": 0, "tibio": 0, "frio": 0, "otros": 0}
    for p in pend:
        c = _cu_cat(p.get("display_category"))
        key = {
            "INMEDIATO": "inmediato",
            "CRÍTICO": "critico",
            "CALIENTE": "caliente",
            "TIBIO": "tibio",
            "FRÍO": "frio",
        }.get(c, "otros")
        t[key] += 1
    return t


def _cu_orden(pend):
    return sorted(
        pend,
        key=lambda p: (
            -_CU_TEMP.get(str(p.get("display_category")), 0),
            -_CU_OPS.get(str(p.get("operational_status")), 0),
            str(p.get("first_detected") or ""),
        ),
    )


def _cu_z(fecha) -> str:
    return fecha.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cu_chile(fecha) -> str:
    from zoneinfo import ZoneInfo

    return fecha.astimezone(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d %H:%M")


def _cu_clave_cron() -> str:
    raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    try:
        meta = (json.loads(raw).get("metadata") or {}) if raw else {}
    except (ValueError, AttributeError):
        meta = {}
    sid = str(meta.get("scheduled_task_id") or "").strip() if isinstance(meta, dict) else ""
    return f"cron:{sid}" if sid else "manual"


def _cu_numero(args, clave, defecto, minimo, maximo):
    try:
        valor = float(args.get(clave))
    except (TypeError, ValueError):
        return defecto
    return max(minimo, min(maximo, valor))


def _cu_huellas(m) -> list[str]:
    claves = []
    imid = str(m.get("internet_message_id") or m.get("internetMessageId") or "").strip().lower()
    if imid:
        claves.append("imid:" + imid)
    mid = str(m.get("id") or m.get("message_id") or "").strip()
    if mid:
        claves.append("id:" + mid)
    return [hashlib.sha256(c.encode("utf-8")).hexdigest()[:20] for c in claves]


def _cu_calcular_ventana(datos, args, ahora, clave):
    horas = _cu_numero(args, "ventana_horas", None, 1, _CU_ENTREGADOS_DIAS * 24 - 24)
    tope_h = horas or _CU_VENTANA_MAX_HORAS
    sin_marca_h = horas or _CU_SIN_MARCA_HORAS
    solapamiento = _cu_numero(args, "solapamiento_min", _CU_SOLAPAMIENTO_MIN, 0, 720)
    marca = _estado_fecha(((datos.get("marcas") or {}).get(clave) or {}).get("hasta"), None)
    limite = ahora - timedelta(hours=tope_h)
    desde = marca - timedelta(minutes=solapamiento) if marca else ahora - timedelta(hours=sin_marca_h)
    return {
        "clave": clave,
        "marca": marca,
        "desde": max(desde, limite),
        "limite": limite,
        "recortada": desde < limite,
        "tope_h": tope_h,
        "sin_marca_h": sin_marca_h,
        "solapamiento_min": solapamiento,
    }


def _cu_describir_ventana(v) -> str:
    if v["marca"]:
        texto = (
            f"desde la última corrida confirmada de esta tarea ({_cu_chile(v['marca'])} Chile) menos "
            f"{int(v['solapamiento_min'])} min de solapamiento"
        )
    else:
        texto = f"sin corrida confirmada previa de esta tarea: últimas {int(v['sin_marca_h'])} h"
    if v["recortada"]:
        texto += (
            f"; recortada al máximo de {int(v['tope_h'])} h: lo recibido antes de {_cu_chile(v['limite'])} "
            "Chile no se lee"
        )
    return texto


_CU_VISTA_MAX_FILAS = 80
_CU_SIN_NO_LEIDOS = "_(sin correos no leídos en las últimas {horas} h)_"


def _cu_vista_no_leidos(args, datos) -> int:
    valor = args.get("vista_no_leidos_horas")
    if valor in (None, ""):
        valor = datos.get("vista_no_leidos_horas")
    try:
        horas = int(float(valor))
    except (TypeError, ValueError):
        return 0
    return max(0, min(horas, _CU_VENTANA_MAX_HORAS))


def _cu_desde_con_vista(v, ahora, horas_vista):
    if not horas_vista:
        return v["desde"]
    return max(min(v["desde"], ahora - timedelta(hours=horas_vista)), v["limite"])


def _cu_ventana(args):
    from zoneinfo import ZoneInfo

    ahora = _estado_fecha(args.get("ahora"), datetime.now(UTC))
    datos = _cu_load()
    v = _cu_calcular_ventana(datos, args, ahora, _cu_clave_cron())
    horas_vista = _cu_vista_no_leidos(args, datos)
    if horas_vista:
        v = {**v, "desde": _cu_desde_con_vista(v, ahora, horas_vista)}
    salida = {
        "success": True,
        "since": _cu_z(v["desde"]),
        "hasta": _cu_z(ahora),
        "desde_chile": _cu_chile(v["desde"]),
        "hasta_chile": _cu_chile(ahora),
        "ventana_para_el_informe": f"desde {_cu_chile(v['desde'])[11:]} CL del {_cu_chile(v['desde'])[8:10]}-"
        f"{_cu_chile(v['desde'])[5:7]} hasta {_cu_chile(ahora)[11:]} CL (usa estas horas tal cual; no conviertas)",
        "count": _CU_PAGINA,
        "cron": v["clave"],
        "modo": _cu_describir_ventana(v),
        "recortada": v["recortada"],
        "hora_chile": ahora.astimezone(ZoneInfo("America/Santiago")).strftime("%A %Y-%m-%d %H:%M"),
        "pendientes_guardados": len(datos.get("pending", [])),
        "pendientes_de_clasificar": len(datos.get("por_clasificar", [])),
        "siguiente": (
            "Llama ms365_correo_sync(accion=plan, since=<since>) SIN mensajes: el servidor lee la bandeja completa "
            "de la ventana y te devuelve solo lo nuevo por clasificar."
        ),
    }
    if horas_vista:
        salida["vista_no_leidos_horas"] = horas_vista
        salida["modo"] += (
            f"; vista de no leídos: el informe muestra todos los no leídos de las últimas {horas_vista} h, también "
            "los ya mostrados, hasta que se lean en Outlook"
        )
    return salida


def _cu_sender(m):
    fobj = m.get("from") or m.get("sender")
    if isinstance(fobj, dict):
        fea = fobj.get("emailAddress") if isinstance(fobj.get("emailAddress"), dict) else fobj
        return str(fea.get("name") or fea.get("address") or "").strip(), str(fea.get("address") or "").strip()
    name = str(m.get("sender") or m.get("from") or m.get("sender_name") or m.get("remitente") or "").strip()
    return name, str(m.get("sender_address") or m.get("from_address") or "").strip()


def _cu_norm_msg(m):
    name, addr = _cu_sender(m)
    return {
        "id": str(m.get("id") or m.get("message_id") or "").strip(),
        "internet_message_id": str(m.get("internet_message_id") or m.get("internetMessageId") or "").strip(),
        "subject": str(m.get("subject") or m.get("asunto") or "").strip(),
        "sender": name,
        "sender_address": addr,
        "received": m.get("received") or m.get("receivedDateTime") or m.get("fecha"),
        "is_read": bool(m.get("is_read") or m.get("isRead")),
        "preview": str(m.get("preview") or m.get("bodyPreview") or "")[:160],
        "relacion": str(m.get("_relacion") or m.get("relacion") or ""),
    }


def _cu_mensajes_de(args):
    mensajes = args.get("mensajes")
    if isinstance(mensajes, str):
        try:
            mensajes = json.loads(mensajes)
        except ValueError:
            mensajes = []
    if isinstance(mensajes, dict):
        mensajes = mensajes.get("messages") or mensajes.get("value") or []
    return mensajes if isinstance(mensajes, list) else []


def _cu_ruta_relativa(enlace: str) -> str:
    if enlace.startswith(GRAPH_BASE):
        return enlace[len(GRAPH_BASE) :]
    partes = urllib.parse.urlsplit(enlace)
    ruta = partes.path.split("/v1.0", 1)[1] if "/v1.0" in partes.path else partes.path
    return ruta + (f"?{partes.query}" if partes.query else "")


_CU_AL_USUARIO_SIN_LECTURA = (
    " Al usuario dile que no se pudo leer el buzón y, si lo hay, el código de Microsoft 365, sin nombrar "
    "herramientas, acciones ni pasos internos. Las demás herramientas de correo leen el mismo buzón y fallarán "
    "igual: no las ofrezcas como alternativa."
)


def _cu_leer_bandeja(token, desde, ahora, max_paginas):
    ruta = (
        f"/me/mailFolders/inbox/messages?$top={_CU_PAGINA}&$select=id,internetMessageId,subject,from,"
        "receivedDateTime,bodyPreview,isRead,toRecipients,ccRecipients"
        f"&$filter=receivedDateTime%20ge%20{_cu_z(desde)}&$orderby=receivedDateTime%20asc"
    )
    mensajes, vistos, paginas, completa, orden = [], set(), 0, False, []
    while ruta and paginas < max_paginas:
        resultado = _graph("GET", ruta, token)
        if "error" in resultado:
            if not paginas:
                detalle = str(resultado.get("_graph_code") or resultado.get("error") or "").strip()[:160]
                return None, "no pude leer el buzón" + (f" (Microsoft 365: {detalle})" if detalle else ""), None
            break
        paginas += 1
        lote = [m for m in resultado.get("value", []) if isinstance(m, dict)]
        nuevos_lote = [m for m in lote if str(m.get("id") or id(m)) not in vistos]
        if lote and not nuevos_lote:
            break
        for m in nuevos_lote:
            vistos.add(str(m.get("id") or id(m)))
            mensajes.append(m)
            orden.append(_estado_fecha(m.get("receivedDateTime"), None))
        siguiente = resultado.get("@odata.nextLink")
        if not siguiente:
            completa = True
            break
        ruta = _cu_ruta_relativa(str(siguiente))
    fechas = [f for f in orden if f]
    ascendente = fechas == sorted(fechas)
    if completa:
        hasta = ahora
    elif fechas and ascendente:
        hasta = max(fechas)
    else:
        hasta = None
    return mensajes, None, {"paginas": paginas, "completa": completa, "hasta": hasta, "ascendente": ascendente}


def _cu_mensajes_servidor(args, ahora, desde=None):
    if desde is None:
        desde = _estado_fecha(args.get("since"), None) or (
            ahora.replace(minute=0, second=0, microsecond=0) - timedelta(hours=_CU_SIN_MARCA_HORAS)
        )
    user_id = _task_user_id()
    if not user_id:
        return None, "sin token de Microsoft 365: la tarea no trae el usuario dueño del correo", None
    token, motivo = _token_herramienta(user_id)
    if not token:
        return None, f"sin token de Microsoft 365: {motivo or 'el servidor no devolvió token'}", None
    max_paginas = int(_cu_numero(args, "max_paginas", _CU_PAGINAS_MAX, 1, 50))
    mensajes, error, lectura = _cu_leer_bandeja(token, desde, ahora, max_paginas)
    if error:
        return None, error, None
    yo = _graph("GET", "/me?$select=mail,userPrincipalName", token)
    propias = {str(yo.get(k) or "").lower() for k in ("mail", "userPrincipalName")} - {""}
    if propias:
        for m in mensajes:
            m["_relacion"] = _cu_relacion_destinatarios(m, propias)
    return mensajes, None, lectura


def _cu_relacion_destinatarios(mensaje, propias) -> str:
    def direcciones(clave):
        return {str((r.get("emailAddress") or {}).get("address") or "").lower() for r in mensaje.get(clave) or []}

    if direcciones("toRecipients") & propias:
        return "Para mí"
    if direcciones("ccRecipients") & propias:
        return "En copia"
    return "No dirigido"


def _cu_sin_leer_en_bandeja() -> int | None:
    user_id = _task_user_id()
    if not user_id:
        return None
    token, _ = _token_herramienta(user_id)
    if not token:
        return None
    resultado = _graph("GET", "/me/mailFolders/inbox?$select=unreadItemCount", token)
    cantidad = resultado.get("unreadItemCount") if isinstance(resultado, dict) else None
    return cantidad if isinstance(cantidad, int) and not isinstance(cantidad, bool) else None


def _cu_plan(args):
    ahora = _estado_fecha(args.get("ahora"), datetime.now(UTC))
    datos = _cu_load()
    conocidos = set()
    for sec in ("pending", "resolved", "discarded"):
        for it in datos.get(sec, []):
            conocidos.add(_cu_key(it))
    entregados = datos.get("entregados") or {}
    clave = _cu_clave_cron()
    nuevos, mapa, diferidos = [], {}, []
    n = 0
    lectura = {"cron": clave, "desde": None, "hasta": None, "completa": None, "paginas": 0, "recortada": False}
    horas_vista = 0
    if "mensajes" in args:
        crudos = _cu_mensajes_de(args)
        lectura["origen"] = "lista entregada por el modelo: la marca de agua no avanza"
    else:
        v = _cu_calcular_ventana(datos, args, ahora, clave)
        desde = v["desde"]
        explicito = _estado_fecha(args.get("since"), None)
        if explicito and (not v["marca"] or explicito < desde):
            v["recortada"] = v["recortada"] or explicito < v["limite"]
            desde = max(explicito, v["limite"])
        horas_vista = _cu_vista_no_leidos(args, datos)
        desde = _cu_desde_con_vista({**v, "desde": desde}, ahora, horas_vista)
        crudos, error, leido = _cu_mensajes_servidor(args, ahora, desde)
        if error:
            return {"success": False, "error": f"ms365_correo_sync(plan): {error}.{_CU_AL_USUARIO_SIN_LECTURA}"}
        lectura.update(
            desde=_cu_z(desde),
            hasta=_cu_z(leido["hasta"]) if leido["hasta"] else None,
            completa=leido["completa"],
            paginas=leido["paginas"],
            recortada=v["recortada"],
            limite=_cu_z(v["limite"]),
            origen=_cu_describir_ventana({**v, "desde": desde}),
        )
    previos = sorted(
        (p for p in datos.get("por_clasificar") or [] if isinstance(p, dict)),
        key=lambda p: str(p.get("received") or ""),
    )
    leidos = sorted(
        (_cu_norm_msg(c if isinstance(c, dict) else {}) for c in crudos), key=lambda m: str(m.get("received") or "")
    )
    lectura["mensajes_leidos"] = len(leidos)
    lectura["sin_leer_en_la_lectura"] = sum(1 for m in leidos if not m.get("is_read"))
    lectura["ultimo_recibido"] = max((str(m.get("received") or "") for m in leidos), default=None)
    tomadas = set()
    for m in previos + leidos:
        if not m.get("id") and not m.get("subject"):
            continue
        m["id"] = _resolve_message_id(m["id"]) if m.get("id") else m.get("id", "")
        if horas_vista and m.get("is_read"):
            continue
        huellas = _cu_huellas(m)
        if any(h in entregados or h in tomadas for h in huellas):
            continue
        k = _cu_key({"message_id": m["id"], "subject": m["subject"]})
        if m["id"] and k in conocidos:
            continue
        tomadas.update(huellas)
        if m["id"]:
            conocidos.add(k)
        if n >= _CU_TOP_FILAS:
            diferidos.append(m)
            continue
        n += 1
        tok = f"N{n}"
        mapa[tok] = m
        nuevos.append(
            {
                "token": tok,
                "id": m["id"],
                "subject": m["subject"],
                "sender": m["sender"],
                "preview": m["preview"],
                "received": m["received"],
                "is_read": m["is_read"],
                "relacion": m.get("relacion") or "",
            }
        )
    vista = None
    if horas_vista:
        corte = ahora - timedelta(hours=horas_vista)
        no_leidos = []
        for m in leidos:
            recibido = _estado_fecha(m.get("received"), None)
            if m.get("is_read") or not recibido or recibido < corte:
                continue
            no_leidos.append(
                {
                    "id": m.get("id") or "",
                    "internet_message_id": m.get("internet_message_id") or "",
                    "received": m.get("received"),
                }
            )
        vista = {"horas": horas_vista, "no_leidos": no_leidos}
        total_sin_leer = _cu_sin_leer_en_bandeja()
        if total_sin_leer is not None:
            vista["antiguos_sin_leer"] = max(0, total_sin_leer - len(no_leidos))
    plan_path = _cu_plan_path()
    _cu_limpiar_planes(plan_path)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    _cu_escribir_atomico(
        plan_path,
        json.dumps(
            {
                "task_id": _current_task_id(),
                "revision": datos.get("revision", 0),
                "ahora": _cu_z(ahora),
                "mapa": mapa,
                "diferidos": diferidos,
                "lectura": lectura,
                **({"vista_no_leidos": vista} if vista else {}),
            },
            ensure_ascii=False,
        ),
    )
    pend = datos.get("pending", [])
    salida = {
        "success": True,
        "revision_estado": datos.get("revision", 0),
        "nuevos_sin_clasificar": nuevos,
        "ventana_leida": lectura,
        "digest_parcial": {
            "totales": _cu_totales(pend),
            "pendientes": len(pend),
            "frios_consolidados": (datos.get("frios_consolidados") or {}).get("count", 0),
        },
        "siguiente": (
            "Clasifica TODOS y SOLO los nuevos_sin_clasificar (por su token N1,N2,…) y llama "
            "ms365_correo_sync(accion=commit, clasificaciones={token: {display_category, base_temperature, "
            "relationship, operational_status, action_type, action_required, requires_response, due_date, "
            "due_status, ruido_automatico}}). Si no hay nuevos, llama commit con clasificaciones={}. "
            "relacion ya viene calculada por el servidor desde Para/CC: no la cambies. due_status: HOY si "
            "pide respuesta durante el día, MAÑANA, FECHA (con due_date YYYY-MM-DD) o SIN_PLAZO. "
            "requires_response=false si es solo informativo. Un token que no clasifiques queda pendiente y "
            "vuelve en la próxima corrida."
        ),
    }
    if vista:
        salida["vista_no_leidos"] = {"horas": horas_vista, "no_leidos_en_la_ventana": len(vista["no_leidos"])}
    avisos = []
    if diferidos:
        avisos.append(
            f"{len(diferidos)} correos nuevos superan el tope de {_CU_TOP_FILAS} por corrida: quedan guardados al "
            "hacer commit y aparecen en la próxima corrida."
        )
    if lectura.get("completa") is False:
        avisos.append(_cu_aviso_incompleta(lectura))
    if avisos:
        salida["aviso"] = " ".join(avisos)
    return salida


def _cu_aviso_incompleta(lectura) -> str:
    if lectura.get("hasta"):
        destino = f"la marca avanza solo hasta {lectura['hasta']} y la próxima corrida continúa desde ahí"
    else:
        destino = "la marca no avanza; los correos más antiguos de la ventana siguen sin leer"
    return (
        f"La bandeja tenía más correos de los que se pudieron leer ({lectura.get('paginas')} páginas de "
        f"{_CU_PAGINA}): {destino}."
    )


def _cu_item_nuevo(m, clasif, ahora, display_id):
    recibido = _estado_fecha(m.get("received"), ahora).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_s = ahora.strftime("%Y-%m-%dT%H:%M:%SZ")
    bt = _cu_cat(clasif.get("base_temperature") or clasif.get("display_category"))
    dc = _cu_cat(clasif.get("display_category"))
    if _CU_TEMP.get(bt, 0) < _CU_TEMP.get(dc, 0):
        bt = dc
    return {
        "display_id": display_id,
        "message_id": _resolve_message_id(str(m.get("id") or "").strip()) if m.get("id") else "",
        "internet_message_id": str(m.get("internet_message_id") or "")[:300],
        "subject": str(m.get("subject") or "")[:200],
        "sender_name": str(m.get("sender") or "")[:120],
        "sender_address": str(m.get("sender_address") or "")[:160],
        "relationship": str(m.get("relacion") or clasif.get("relationship") or ""),
        "display_category": dc,
        "base_temperature": bt,
        "operational_status": _cu_ops_norm(clasif.get("operational_status")),
        "action_type": str(clasif.get("action_type") or ""),
        "action_required": str(clasif.get("action_required") or "")[:160],
        "requires_response": bool(_cu_rr_true(clasif)),
        "due_status": str(clasif.get("due_status") or "SIN_PLAZO"),
        "due_date": clasif.get("due_date"),
        "first_detected": recibido,
        "last_reviewed": now_s,
        "_ruido": bool(clasif.get("ruido_automatico")),
    }


def _cu_slim(item):
    return {k: item.get(k) for k in _CU_CAMPOS}


def _cu_digest(datos, pend, ahora, now_s, frios_count, nuevos_n, movidos, archivo_kb, aviso, ignoradas, mostrados=()):
    from zoneinfo import ZoneInfo

    ordenados = _cu_orden(pend)[:_CU_TOP_FILAS]
    filas = [_cu_fila(p, ahora) for p in ordenados]
    items_nuevos = list(mostrados)
    filas_nuevas = [_cu_fila(p, ahora) for p in _cu_orden(items_nuevos)]
    out = {
        "success": True,
        "modo": datos.get("_modo") or "hourly",
        "hora_chile": ahora.astimezone(ZoneInfo("America/Santiago")).strftime("%A %Y-%m-%d %H:%M"),
        "totales": _cu_totales(pend),
        "totales_nuevos": _cu_totales(items_nuevos),
        "filas_nuevas": [f for f in filas_nuevas if f["categoria"] in _CU_PRINCIPALES],
        "filas": [f for f in filas if f["categoria"] in _CU_PRINCIPALES],
        "otros_relevantes": [f for f in filas_nuevas if f["categoria"] not in _CU_PRINCIPALES],
        "frios_consolidados": frios_count,
        "nuevos_este_run": nuevos_n,
        "resueltos_este_run": movidos,
        "archivo_kb": archivo_kb,
        "aviso": aviso,
        "clasificaciones_ignoradas": ignoradas,
        "tablas_md": _cu_tablas_md(filas_nuevas),
        "orden_visible": _cu_orden_visible(filas_nuevas),
        "siguiente": (
            "Formatea el informe con la plantilla EXACTA de la skill. Bajo cada categoría pega TAL CUAL "
            "la tabla de tablas_md (ya trae las columnas ID, Edad, Asunto, Remitente, Relación, Acción "
            "requerida, Plazo y Respuesta): no la rehagas ni cambies columnas. Conteos y tablas por categoría "
            "salen de totales_nuevos y filas_nuevas (solo lo de esta ventana). totales es el acumulado: "
            "va solo en la línea de totales acumulados. No inventes IDs: usa solo el campo id de las filas. "
            "NO abras ni modifiques el estado: ya quedó actualizado."
        ),
    }
    return out


_CU_COLUMNAS = (
    ("ID", "id"),
    ("Edad", "edad"),
    ("Asunto", "asunto"),
    ("Remitente", "remitente"),
    ("Relación", "relacion"),
    ("Acción requerida", "accion"),
    ("Plazo", "plazo"),
    ("Respuesta", "respuesta"),
)
_CU_SIN_CORREOS = "_(sin correos nuevos en esta ventana)_"


def _cu_celda(valor) -> str:
    texto = re.sub(r"\s+", " ", str(valor if valor not in (None, "") else "—")).strip()
    return texto.replace("|", "/")


def _cu_orden_visible(filas) -> list[str]:
    return [
        str(f.get("id"))
        for categoria in ("INMEDIATO", "CRÍTICO", "CALIENTE", "TIBIO", "FRÍO")
        for f in filas
        if _cu_cat(f.get("categoria")) == categoria and f.get("id")
    ]


def _cu_tablas_md(filas) -> dict:
    tablas = {}
    for categoria in ("INMEDIATO", "CRÍTICO", "CALIENTE", "TIBIO", "FRÍO"):
        propias = [f for f in filas if _cu_cat(f.get("categoria")) == categoria]
        if not propias:
            tablas[categoria] = _CU_SIN_CORREOS
            continue
        lineas = [
            "| " + " | ".join(t for t, _ in _CU_COLUMNAS) + " |",
            "|" + "|".join("---" for _ in _CU_COLUMNAS) + "|",
        ]
        lineas += ["| " + " | ".join(_cu_celda(f.get(k)) for _, k in _CU_COLUMNAS) + " |" for f in propias]
        tablas[categoria] = "\n".join(lineas)
    return tablas


def _cu_tamano(valor) -> int:
    return len(json.dumps(valor, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _cu_aplicar_tope(datos, now_s):
    limite = _CU_MAX_KB * 1024
    total = _cu_tamano({k: datos.get(k) or [] for k in _CU_SECCIONES_TOPE})
    if total <= limite:
        return 0, False
    podables = []
    for sec in ("resolved", "discarded", "history"):
        for it in datos.get(sec) or []:
            if sec == "resolved" and str(it.get("resolved_at") or "") == now_s:
                continue
            fecha = str(
                it.get("resolved_at") or it.get("discarded_at") or it.get("date") or it.get("last_reviewed") or ""
            )
            podables.append((fecha, sec, id(it), it))
    podables.sort(key=lambda t: (t[0], t[1]))
    fuera = {}
    podables = [t for t in podables if not (t[1] == "resolved" and str(t[3].get("mostrado_at") or "") == now_s)]
    for _fecha, sec, ident, it in podables:
        if total <= limite:
            break
        total -= _cu_tamano(it) + 1
        fuera.setdefault(sec, set()).add(ident)
    for sec, idents in fuera.items():
        datos[sec] = [it for it in datos.get(sec) or [] if id(it) not in idents]
    retirados = 0
    if total > limite:
        degradables = sorted(
            [
                p
                for p in datos.get("pending") or []
                if _cu_cat(p.get("display_category")) in ("TIBIO", "FRÍO")
                and _cu_ops_norm(p.get("operational_status")) == "SIN_ACCIÓN"
                and not _cu_rr_true(p)
                and not _cu_has_plazo(p)
            ],
            key=lambda p: str(p.get("first_detected") or ""),
        )
        quitar = set()
        for p in degradables:
            if total <= limite:
                break
            total -= _cu_tamano(p) + 1
            quitar.add(id(p))
        datos["pending"] = [p for p in datos.get("pending") or [] if id(p) not in quitar]
        retirados = len(quitar)
    return retirados, total > limite


def _cu_repetidos_no_leidos(datos, vista, mostrados, ahora, now_s):
    por_id, por_imid = {}, {}
    for sec in ("resolved", "pending"):
        for it in datos.get(sec) or []:
            if not isinstance(it, dict) or not it.get("display_id"):
                continue
            if sec == "resolved" and it.get("resolution_type") != "mostrado":
                continue
            if it.get("message_id"):
                por_id[str(it["message_id"])] = it
            imid = str(it.get("internet_message_id") or "").strip().lower()
            if imid:
                por_imid[imid] = it
    vistos = {str(p.get("message_id")) for p in mostrados if p.get("message_id")}
    repetidos, sin_registro = [], []
    for m in vista.get("no_leidos") or []:
        mid = str(m.get("id") or "")
        if mid and mid in vistos:
            continue
        it = por_id.get(mid) or por_imid.get(str(m.get("internet_message_id") or "").strip().lower())
        if it is None:
            sin_registro.append(m)
            continue
        if str(it.get("message_id")) in vistos:
            continue
        vistos.update({mid, str(it.get("message_id"))})
        it["mostrado_at"] = now_s
        copia = dict(it)
        copia["display_category"] = _cu_display_category(copia, ahora)
        repetidos.append(copia)
    return repetidos, sin_registro


def _cu_actualizar_por_clasificar(datos, agregar, ahora) -> int:
    entregados = datos.get("entregados") or {}
    hoy = ahora.strftime("%Y-%m-%d")
    limite = (ahora - timedelta(days=_CU_ENTREGADOS_DIAS)).strftime("%Y-%m-%d")
    cola, vistas, descartados = [], set(), 0
    for m in list(datos.get("por_clasificar") or []) + list(agregar):
        if not isinstance(m, dict):
            continue
        huellas = _cu_huellas(m)
        if any(h in entregados for h in huellas):
            continue
        if huellas and any(h in vistas for h in huellas):
            continue
        vistas.update(huellas)
        item = dict(m)
        item.setdefault("pendiente_desde", hoy)
        if str(item["pendiente_desde"]) < limite:
            descartados += 1
            continue
        cola.append(item)
    cola.sort(key=lambda m: str(m.get("received") or ""))
    if len(cola) > _CU_POR_CLASIFICAR_MAX:
        descartados += len(cola) - _CU_POR_CLASIFICAR_MAX
        cola = cola[-_CU_POR_CLASIFICAR_MAX:]
    datos["por_clasificar"] = cola
    return descartados


def _cu_hecho_path() -> Path:
    plan = _cu_plan_path()
    return plan.with_name(f"{plan.stem}.hecho.json")


def _cu_plan_propio_existe() -> bool:
    scratch = _cu_plan_path()
    plan = _cu_leer_json(scratch) if scratch.is_file() else None
    return bool(plan) and str(plan.get("task_id") or "") == _current_task_id()


def _cu_commit_sin_plan(args):
    hecho = _cu_hecho_path()
    previo = _cu_leer_json(hecho) if hecho.is_file() else None
    if isinstance(previo, dict) and isinstance(previo.get("digest"), dict):
        if str(previo.get("task_id") or "") == _current_task_id():
            digest = dict(previo["digest"])
            digest["repetido"] = True
            nota = (
                f"Este commit ya se hizo en esta ejecución ({digest.get('hora_chile') or 'antes'}): es el mismo "
                "resultado y no se registró nada nuevo. No vuelvas a llamar commit; entrega este informe."
            )
            if args.get("clasificaciones"):
                nota += " Las clasificaciones de esta llamada no se aplicaron."
            digest["aviso"] = f"{nota} {digest['aviso']}" if digest.get("aviso") else nota
            return digest
    return {
        "success": False,
        "sin_lectura": True,
        "error": (
            "ms365_correo_sync(commit): esta ejecución no leyó la bandeja (accion=plan falló o no se llamó), así que "
            "no hay nada que registrar y no se registró nada. No entregues un informe con ceros ni digas «sin correos "
            "nuevos»: informa que no se pudo leer el buzón, con el código de Microsoft 365 si plan lo dio, sin nombrar "
            "herramientas, acciones ni pasos internos."
        ),
    }


def _cu_guardar_hecho(digest) -> None:
    try:
        _cu_escribir_atomico(
            _cu_hecho_path(),
            json.dumps({"task_id": _current_task_id(), "digest": digest}, ensure_ascii=False, default=str),
        )
    except OSError:
        pass


def _cu_commit(args):
    if not _cu_plan_propio_existe():
        return _cu_commit_sin_plan(args)
    ahora = _estado_fecha(args.get("ahora"), datetime.now(UTC))
    now_s = ahora.strftime("%Y-%m-%dT%H:%M:%SZ")
    _, respondidos = _estado_no_leidos() if args.get("verificar_leidos", True) else (None, None)
    with _cu_bloqueo():
        r = _cu_commit_estado(args, ahora, now_s, respondidos)
    if "abort" in r:
        return r["abort"]
    datos, pend, mostrados, lectura, clave = r["datos"], r["pend"], r["mostrados"], r["lectura"], r["clave"]
    declarado = _cu_marcado_declarado(args, datos)
    categorias_leidas = _cu_categorias_a_leer(datos) if declarado else set()
    a_marcar = [
        p.get("message_id")
        for p in r.get("informe", mostrados)
        if p.get("message_id") and _cu_cat_informe(p.get("display_category")) in categorias_leidas
    ]
    if "FRÍO" in categorias_leidas:
        a_marcar += [mid for mid in r.get("frios_mids") or [] if mid not in a_marcar]
    marcados = _cu_marcar_leidos(a_marcar) if declarado else None
    try:
        _remember_ids([{"id": p.get("message_id")} for p in pend if p.get("message_id")])
    except Exception:  # noqa: BLE001
        pass
    datos["_modo"] = "daily" if (args.get("mode") or "").lower().startswith("d") else "hourly"
    digest = _cu_digest(
        datos,
        pend,
        ahora,
        now_s,
        r["frios"],
        r["altas"],
        r["movidos"],
        r["archivo_kb"],
        r["aviso"],
        r["ignoradas"],
        r.get("informe", mostrados),
    )
    vista = r.get("vista_no_leidos")
    if vista:
        digest["vista_no_leidos"] = vista
        vacio = _CU_SIN_NO_LEIDOS.format(horas=vista["horas"])
        digest["tablas_md"] = {k: (vacio if v == _CU_SIN_CORREOS else v) for k, v in digest["tablas_md"].items()}
        digest["totales"] = digest["totales_nuevos"]
        digest.pop("filas", None)
        n_marcados = int((marcados or {}).get("marcados") or 0) if isinstance(marcados, dict) else 0
        sobre_leidos = (
            f"Se marcaron como leídos {n_marcados} correos "
            f"{', '.join(sorted(categorias_leidas, key=_CU_CATEGORIAS.index))} (marcados_leidos): dilo con esa cifra; "
            "los demás siguen sin leer en Outlook. "
            if n_marcados
            else "No digas que se marcaron como leídos: siguen sin leer en Outlook. "
        )
        from zoneinfo import ZoneInfo

        hasta = ahora.astimezone(ZoneInfo(_ZONA_USUARIO)).strftime("%H:%M")
        cats = ", ".join(sorted(categorias_leidas, key=_CU_CATEGORIAS.index))
        digest["linea_vista"] = (
            f"Ventana: no leídos de las últimas {vista['horas']} h (hasta {hasta} CL). Nuevos: {vista['nuevos']} · "
            f"Siguen sin leer de antes: {vista['siguen_sin_leer']}"
            + (f" ({n_marcados} {cats} marcados como leídos)." if n_marcados else ".")
        )
        sobre_leidos = (
            "La segunda línea del informe es EXACTAMENTE linea_vista (cópiala tal cual, con sus cifras). "
            + sobre_leidos
        )
        digest["siguiente"] = (
            "Formatea el informe con la plantilla de la skill. VISTA DE NO LEÍDOS: bajo cada categoría pega TAL CUAL "
            f"la tabla de tablas_md; traen TODOS los correos sin leer de las últimas {vista['horas']} h, también los "
            "que ya mostró un informe anterior (conservan su #ID). Los conteos salen SOLO de totales (son los de "
            "estas tablas); no hay otros acumulados que informar. En el encabezado di cuántos son nuevos "
            "(vista_no_leidos.nuevos) y cuántos siguen sin leer de antes (vista_no_leidos.siguen_sin_leer). "
            "No inventes IDs ni filas. " + sobre_leidos + "NO abras ni modifiques el estado: ya quedó actualizado."
        )
    digest["revision_estado"] = datos.get("revision")
    digest["ya_entregados_por_otra_ejecucion"] = r["ya_entregados"]
    digest["ventana_leida"] = {
        **{
            k: lectura.get(k)
            for k in (
                "cron",
                "desde",
                "hasta",
                "completa",
                "paginas",
                "recortada",
                "origen",
                "mensajes_leidos",
                "sin_leer_en_la_lectura",
                "ultimo_recibido",
            )
        },
        "marca_confirmada_hasta": (datos["marcas"].get(clave) or {}).get("hasta") if clave else None,
    }
    digest["pendientes_de_clasificar"] = len(datos.get("por_clasificar") or [])
    digest["sin_clasificar_este_run"] = r["sin_clasificar"]
    if marcados is not None:
        digest["marcados_leidos"] = marcados
    elif args.get("marcar_leidos") is not False:
        digest["marcado_leidos"] = "desactivado: nadie lo declaró; los correos del informe quedan sin leer en Outlook"
    digest["categorias_que_se_marcan_leidas"] = sorted(categorias_leidas, key=_CU_CATEGORIAS.index)
    _cu_guardar_hecho(digest)
    return digest


def _cu_commit_estado(args, ahora, now_s, respondidos):
    datos = _cu_load()
    scratch = _cu_plan_path()
    plan = _cu_leer_json(scratch) if scratch.is_file() else None
    plan = plan if plan and str(plan.get("task_id") or "") == _current_task_id() else {}
    mapa = plan.get("mapa") if isinstance(plan.get("mapa"), dict) else {}
    diferidos = [d for d in plan.get("diferidos") or [] if isinstance(d, dict)]
    lectura = plan.get("lectura") if isinstance(plan.get("lectura"), dict) else {}
    clasifs = args.get("clasificaciones") or {}
    if isinstance(clasifs, str):
        try:
            clasifs = json.loads(clasifs)
        except ValueError:
            clasifs = {}
    if not isinstance(clasifs, dict):
        clasifs = {}
    clasificados = set()
    entregados = datos["entregados"]
    pend = list(datos.get("pending") or [])
    frios_nuevos = 0
    altas = 0
    ignoradas = []
    nuevos_mids = []
    frios_mids = []
    next_id = int(datos.get("next_display_id") or 1)
    conocidos = {
        _cu_key(it) for sec in ("pending", "resolved", "discarded") for it in datos.get(sec, []) if isinstance(it, dict)
    }
    ya_entregados = set()
    for tok, m in mapa.items():
        if not isinstance(m, dict):
            continue
        k = _cu_key({"message_id": m.get("id"), "subject": m.get("subject")})
        if any(h in entregados for h in _cu_huellas(m)) or (
            _cu_pick_id({"message_id": m.get("id")}) and k in conocidos
        ):
            ya_entregados.add(tok)

    for tok, clasif in clasifs.items():
        m = mapa.get(tok)
        if not m or not isinstance(clasif, dict):
            ignoradas.append(tok)
            continue
        clasificados.add(tok)
        if tok in ya_entregados:
            continue
        for h in _cu_huellas(m):
            entregados[h] = ahora.strftime("%Y-%m-%d")
        tmp = _cu_item_nuevo(m, clasif, ahora, f"#{next_id:02d}")
        if _cu_es_frio_inerte(tmp) and (tmp.get("_ruido") or _cu_remitente_auto(tmp)):
            frios_nuevos += 1
            if tmp.get("message_id"):
                frios_mids.append(tmp["message_id"])
            continue
        pend.append(_cu_slim(tmp))
        if tmp.get("message_id"):
            nuevos_mids.append(tmp["message_id"])
        altas += 1
        next_id += 1

    pend, degradados = _cu_dedup(pend)
    if degradados:
        return {"abort": _cu_abort(datos, pend, ahora, now_s, "merge degradaría una señal fuerte", degradados)}
    vistos = {}
    dup = []
    for p in pend:
        did = str(p.get("display_id"))
        if did in vistos and vistos[did] != p.get("message_id"):
            dup.append(did)
        vistos[did] = p.get("message_id")
    if dup:
        return {"abort": _cu_abort(datos, pend, ahora, now_s, "display_id duplicado entre correos distintos", dup)}

    movidos = []
    if respondidos and not plan.get("vista_no_leidos"):
        quedan = []
        for p in pend:
            if p.get("message_id") and p.get("message_id") in respondidos:
                reg = dict(p)
                reg["resolved_at"] = now_s
                reg["resolution_type"] = "respondido"
                datos.setdefault("resolved", []).append(reg)
                movidos.append(p.get("display_id"))
            else:
                quedan.append(p)
        pend = quedan

    for p in pend:
        p["display_category"] = _cu_display_category(p, ahora)
        p["last_reviewed"] = now_s

    quedan = []
    for p in pend:
        if _cu_es_frio_inerte(p) and _cu_remitente_auto({"sender_name": p.get("sender_name")}):
            frios_nuevos += 1
        else:
            quedan.append(_cu_slim(p))
    pend = quedan

    _cu_purgar(datos, ahora)

    datos["pending"] = pend
    datos["next_display_id"] = next_id
    fc = datos.get("frios_consolidados") or {"count": 0}
    fc["count"] = int(fc.get("count") or 0) + frios_nuevos
    fc["updated_at"] = now_s
    datos["frios_consolidados"] = fc
    datos["last_run"] = now_s
    datos["last_hourly_run"] = now_s

    set_nuevos = set(nuevos_mids)
    mostrados = [p for p in pend if p.get("message_id") in set_nuevos]
    pend = [p for p in pend if p.get("message_id") not in set_nuevos]
    for p in mostrados:
        reg = dict(p)
        reg["resolved_at"] = now_s
        reg["resolution_type"] = "mostrado"
        reg["mostrado_at"] = now_s
        datos.setdefault("resolved", []).append(reg)
    datos["pending"] = pend

    vista = plan.get("vista_no_leidos") if isinstance(plan.get("vista_no_leidos"), dict) else None
    informe, resumen_vista = list(mostrados), None
    if vista:
        repetidos, sin_registro = _cu_repetidos_no_leidos(datos, vista, mostrados, ahora, now_s)
        informe = _cu_orden(mostrados + repetidos)
        fuera = max(0, len(informe) - _CU_VISTA_MAX_FILAS)
        informe = informe[:_CU_VISTA_MAX_FILAS]
        entregados_ahora = datos.get("entregados") or {}
        consolidados = sum(
            1
            for m in sin_registro
            if any(
                h in entregados_ahora
                for h in _cu_huellas({"id": m.get("id"), "internet_message_id": m.get("internet_message_id")})
            )
        )
        resumen_vista = {
            "horas": int(vista.get("horas") or 0),
            "nuevos": len(mostrados),
            "siguen_sin_leer": len(repetidos),
            "frios_consolidados_sin_leer": consolidados,
            "por_clasificar_aun": len(sin_registro) - consolidados,
            "fuera_del_informe_por_tope": fuera,
            "antiguos_sin_leer": vista.get("antiguos_sin_leer"),
        }

    avisos = []
    retirados, sigue_sobre = _cu_aplicar_tope(datos, now_s)
    pend = datos["pending"]
    if retirados:
        fc["count"] = int(fc.get("count") or 0) + retirados
        avisos.append(f"Estado sobre {_CU_MAX_KB} KB: {retirados} TIBIO/FRÍO inertes antiguos pasaron a conteo.")
    if sigue_sobre:
        avisos.append(f"Sigue sobre {_CU_MAX_KB} KB con pendientes accionables (no se degradan).")

    if not plan and clasifs:
        avisos.append(
            "Esta ejecución no tiene plan propio (llama accion=plan en esta misma tarea): no se aceptan tokens de "
            "otra ejecución y no se registró ningún correo."
        )
    if ya_entregados:
        avisos.append(
            f"{len(ya_entregados)} correos de este plan ya los entregó otra ejecución (cron o chat) mientras "
            "clasificabas: no se repiten en este informe."
        )
    if plan and int(plan.get("revision") or 0) != int(datos.get("revision") or 0):
        avisos.append(
            f"El estado cambió entre el plan (revisión {plan.get('revision')}) y el commit (revisión "
            f"{datos.get('revision')}): se recalculó sobre el estado actual sin pisar lo que escribió la otra ejecución."
        )
    sin_clasificar = [
        m for tok, m in mapa.items() if tok not in clasificados and tok not in ya_entregados and isinstance(m, dict)
    ]
    descartados_cola = _cu_actualizar_por_clasificar(datos, sin_clasificar + diferidos, ahora)
    if sin_clasificar:
        avisos.append(
            f"{len(sin_clasificar)} correos leídos en esta corrida no se clasificaron: quedan pendientes y "
            "reaparecen en la próxima corrida."
        )
    if diferidos:
        avisos.append(
            f"{len(diferidos)} correos superaron el tope de {_CU_TOP_FILAS} por corrida: quedan pendientes y "
            "aparecen en la próxima corrida."
        )
    if descartados_cola:
        avisos.append(
            f"{descartados_cola} correos pendientes de clasificar se descartaron por antigüedad o por exceso "
            f"(más de {_CU_ENTREGADOS_DIAS} días o más de {_CU_POR_CLASIFICAR_MAX} en cola)."
        )
    if lectura.get("completa") is False:
        avisos.append(_cu_aviso_incompleta(lectura))
    if lectura.get("recortada"):
        avisos.append(f"La ventana se recortó a su máximo: lo recibido antes de {lectura.get('limite')} no se leyó.")
    marca_nueva = _estado_fecha(lectura.get("hasta"), None)
    clave = str(lectura.get("cron") or "")
    if marca_nueva and clave:
        previa = _estado_fecha((datos["marcas"].get(clave) or {}).get("hasta"), None)
        if not previa or marca_nueva > previa:
            datos["marcas"][clave] = {"hasta": _cu_z(marca_nueva), "confirmada": now_s}
    limite_entregas = (ahora - timedelta(days=_CU_ENTREGADOS_DIAS)).strftime("%Y-%m-%d")
    datos["entregados"] = {h: f for h, f in entregados.items() if str(f) >= limite_entregas}
    aviso = " ".join(avisos) or None

    archivo_kb = _cu_escribir(datos)
    if plan:
        try:
            scratch.unlink()
        except OSError:
            pass
    if resumen_vista and resumen_vista["fuera_del_informe_por_tope"]:
        avisos.append(
            f"{resumen_vista['fuera_del_informe_por_tope']} correos no leídos más quedaron fuera del informe por el "
            f"tope de {_CU_VISTA_MAX_FILAS} filas (los de menor prioridad)."
        )
        aviso = " ".join(avisos)
    return {
        "datos": datos,
        "pend": pend,
        "mostrados": mostrados,
        "frios_mids": frios_mids,
        "informe": informe,
        "vista_no_leidos": resumen_vista,
        "lectura": lectura,
        "clave": clave,
        "frios": fc["count"],
        "altas": altas,
        "movidos": movidos,
        "archivo_kb": archivo_kb,
        "aviso": aviso,
        "ignoradas": ignoradas,
        "ya_entregados": len(ya_entregados),
        "sin_clasificar": len(sin_clasificar),
    }


_CU_CATEGORIAS = ["INMEDIATO", "CRÍTICO", "CALIENTE", "TIBIO", "FRÍO", "OTROS"]


def _cu_normalizar_categorias(valor) -> list[str] | None:
    if isinstance(valor, str):
        texto = _cu_sin_tildes(valor)
        if texto in ("todas", "todos", "all"):
            return list(_CU_CATEGORIAS)
        if texto in ("ninguna", "ninguno", "none", ""):
            return []
        valor = [
            v
            for v in re.split(r"[,;/ ]+", valor)
            if _cu_sin_tildes(v) not in ("y", "e", "los", "las", "de", "categoria", "categorias")
        ]
    if not isinstance(valor, list):
        return None
    mapa = {_cu_sin_tildes(c): c for c in _CU_CATEGORIAS}
    mapa.update({"frios": "FRÍO", "tibios": "TIBIO", "calientes": "CALIENTE", "criticos": "CRÍTICO", "otro": "OTROS"})
    elegidas = []
    for v in valor:
        c = mapa.get(_cu_sin_tildes(v))
        if c is None and _cu_sin_tildes(v):
            return None
        if c and c not in elegidas:
            elegidas.append(c)
    return elegidas


def _cu_categorias_a_leer(datos) -> set[str]:
    guardadas = datos.get("marcar_leidos_categorias")
    if not isinstance(guardadas, list):
        return set(_CU_CATEGORIAS)
    return {c for c in guardadas if c in _CU_CATEGORIAS}


def _cu_marcado_declarado(args, datos) -> bool:
    if args.get("marcar_leidos") is False:
        return False
    if _modo_automatico():
        return _marcado_autorizado_tarea()
    return args.get("marcar_leidos") is True or datos.get("marcar_leidos_por_usuario") is True


def _cu_cat_informe(valor) -> str:
    cat = _cu_cat(valor)
    return cat if cat in _CU_CATEGORIAS[:5] else "OTROS"


def _cu_preferencias(args):
    with _cu_bloqueo():
        return _cu_preferencias_estado(args)


def _cu_preferencias_estado(args):
    datos = _cu_load()
    if "marcar_leidos_categorias" in args and _modo_automatico():
        return {
            "success": False,
            "error": "Las preferencias de marcado las elige el usuario en el chat; una tarea automática no las cambia.",
        }
    if "vista_no_leidos_horas" in args:
        if _modo_automatico():
            return {
                "success": False,
                "error": "La vista de no leídos la elige el usuario en el chat; una tarea automática no la cambia.",
            }
        datos["vista_no_leidos_horas"] = _cu_vista_no_leidos(
            {"vista_no_leidos_horas": args["vista_no_leidos_horas"]}, {}
        )
        _cu_escribir(datos)
        horas = datos["vista_no_leidos_horas"]
        return {
            "success": True,
            "vista_no_leidos_horas": horas,
            "mensaje": (
                f"Desde el próximo informe se muestran todos los correos sin leer de las últimas {horas} h, también "
                "los ya mostrados, hasta que se lean en Outlook. No se marcan como leídos."
                if horas
                else "El informe vuelve a mostrar solo los correos nuevos desde la corrida anterior."
            ),
        }
    if "descartar_marca_leido" in args:
        if _modo_automatico():
            return {
                "success": False,
                "error": "Esta preferencia la elige el usuario en el chat; una tarea automática no la cambia.",
            }
        datos["descartar_marca_leido"] = args.get("descartar_marca_leido") is True or str(
            args.get("descartar_marca_leido")
        ).lower() in ("true", "1", "si", "sí")
        _cu_escribir(datos)
        return {
            "success": True,
            "descartar_marca_leido": datos["descartar_marca_leido"],
            "mensaje": (
                "Desde ahora, cuando descartes un correo también se marca como leído en Outlook."
                if datos["descartar_marca_leido"]
                else "Descartar un correo ya no lo marca como leído en Outlook."
            ),
        }
    if "marcar_leidos_categorias" in args:
        elegidas = _cu_normalizar_categorias(args.get("marcar_leidos_categorias"))
        if elegidas is None:
            return {
                "success": False,
                "error": "Categorías no reconocidas. Usa: " + ", ".join(_CU_CATEGORIAS) + ", todas o ninguna.",
            }
        datos["marcar_leidos_categorias"] = elegidas
        datos["marcar_leidos_por_usuario"] = True
        _cu_escribir(datos)
    if datos.get("marcar_leidos_por_usuario") is not True:
        return {
            "success": True,
            "marcar_leidos_categorias": [],
            "quedan_sin_leer": list(_CU_CATEGORIAS),
            "mensaje": "No hay preferencia guardada: el informe no marca ningún correo como leído salvo que el "
            "usuario lo pida o la tarea programada lo declare en su instrucción.",
        }
    actuales = sorted(_cu_categorias_a_leer(datos) & set(_CU_CATEGORIAS), key=_CU_CATEGORIAS.index)
    return {
        "success": True,
        "marcar_leidos_categorias": actuales,
        "quedan_sin_leer": [c for c in _CU_CATEGORIAS if c not in actuales],
        "mensaje": "Desde el próximo informe se marcan como leídos en Outlook solo: "
        + (", ".join(actuales) or "ninguna")
        + ". El resto queda sin leer (pendiente). En tareas programadas solo se marca si su instrucción lo pide.",
    }


def _cu_marcar_leidos_ahora(args):
    if not _marcado_autorizado_tarea():
        return {"success": False, "error": _MARCAR_NO_DECLARADO, "marcados": 0}
    datos = _cu_load()
    mostrados = [r for r in datos.get("resolved", []) if r.get("resolution_type") == "mostrado"]
    if not mostrados:
        return {"success": False, "error": "No hay un informe anterior con correos para marcar."}
    ids = {
        str(x).strip() if str(x).strip().startswith("#") else f"#{str(x).strip()}"
        for x in args.get("display_ids") or []
    }
    if ids:
        elegidos = [r for r in mostrados if r.get("display_id") in ids]
    else:
        categorias = _cu_normalizar_categorias(args.get("categorias"))
        if not categorias:
            return {"success": False, "error": 'Indica categorias (p. ej. ["FRÍO"]) o display_ids.'}

        def _cuando(r):
            return str(r.get("mostrado_at") or r.get("resolved_at") or "")

        ultimo = max(_cuando(r) for r in mostrados)
        elegidos = [
            r for r in mostrados if _cuando(r) == ultimo and _cu_cat_informe(r.get("display_category")) in categorias
        ]
    resultado = _cu_marcar_leidos([r.get("message_id") for r in elegidos if r.get("message_id")])
    return {"success": True, "display_ids": [r.get("display_id") for r in elegidos], **resultado}


def _cu_marcar_leidos(mids):
    if not mids:
        return {"marcados": 0, "fallos": 0}
    user_id = _task_user_id()
    token, _ = _token_herramienta(user_id) if user_id else (None, None)
    if not token:
        return {"marcados": 0, "fallos": len(mids), "error": "sin token de Microsoft 365"}
    marcados = fallos = 0
    for mid in mids:
        resultado = _graph("PATCH", f"/me/messages/{mid}", token, {"isRead": True})
        if "error" in resultado:
            fallos += 1
        else:
            marcados += 1
    return {"marcados": marcados, "fallos": fallos}


def _cu_abort(datos, pend, ahora, now_s, motivo, detalle):
    ordenados = _cu_orden(pend)[:_CU_TOP_FILAS]
    filas = [_cu_fila(p, ahora) for p in ordenados]
    return {
        "success": False,
        "error": f"{motivo}; el estado NO se escribió. Informa en texto que no se actualizó e incluye esto.",
        "detalle": detalle[:10] if isinstance(detalle, list) else detalle,
        "totales": _cu_totales(pend),
        "filas": [f for f in filas if f["categoria"] in _CU_PRINCIPALES],
        "otros_relevantes": [f for f in filas if f["categoria"] not in _CU_PRINCIPALES],
    }


def _cu_norm_display(x):
    s = str(x or "").strip()
    if s and not s.startswith("#"):
        s = "#" + s
    return s


_DISPLAY_ID_RE = re.compile(r"#?\d{1,6}")


def _cu_resolver_id(args):
    did = _cu_norm_display(args.get("display_id"))
    datos = _cu_load()
    for sec in ("pending", "resolved", "discarded"):
        for it in datos.get(sec, []):
            if str(it.get("display_id")) == did:
                return {
                    "success": True,
                    "message_id": it.get("message_id"),
                    "estado_actual": sec,
                    "subject": it.get("subject"),
                }
    return {
        "success": True,
        "message_id": None,
        "estado_actual": "desconocido",
        "subject": None,
        "nota": f"{did} no está en pending/resolved/discarded; no inventes a qué correo corresponde.",
    }


def _cu_resolver(args):
    destino = str(args.get("destino") or "resolved").strip()
    if destino not in ("resolved", "discarded"):
        return {"success": False, "error": "destino debe ser resolved o discarded"}
    ahora = _estado_fecha(args.get("ahora"), datetime.now(UTC))
    now_s = ahora.strftime("%Y-%m-%dT%H:%M:%SZ")
    objetivo = {_cu_norm_display(x) for x in (args.get("display_ids") or []) if str(x).strip()}
    objetivo |= {_resolve_message_id(str(x).strip()) for x in (args.get("ids") or []) if str(x).strip()}
    with _cu_bloqueo():
        salida = _cu_resolver_estado(args, destino, ahora, now_s, objetivo)
        quiere_marcar = args.get("marcar_leido") is True or (
            args.get("marcar_leido") is not False
            and destino == "discarded"
            and _cu_load().get("descartar_marca_leido") is True
        )
    mids = salida.pop("_message_ids", [])
    if quiere_marcar and mids:
        if _modo_automatico():
            salida["marcado_leido"] = {"marcados": 0, "nota": "una tarea automática no marca correos como leídos"}
        else:
            salida["marcado_leido"] = _cu_marcar_leidos(mids)
    elif quiere_marcar and salida.get("movidos", 0) == 0:
        salida["marcado_leido"] = {"marcados": 0, "nota": "no se movió ningún correo"}
    return salida


def _cu_coincide(registro, objetivo) -> bool:
    return str(registro.get("display_id")) in objetivo or str(registro.get("message_id")) in objetivo


def _cu_resolver_estado(args, destino, ahora, now_s, objetivo):
    datos = _cu_load()
    nota = str(args.get("nota"))[:160] if args.get("nota") else None
    sello = "resolved_at" if destino == "resolved" else "discarded_at"
    tipo = "resuelto_por_usuario" if destino == "resolved" else "descartado_por_usuario"
    pend, movidos, encontrados, mids = [], 0, set(), []
    for p in datos.get("pending", []):
        if _cu_coincide(p, objetivo):
            if p.get("message_id"):
                mids.append(p["message_id"])
            reg = dict(p)
            reg[sello] = now_s
            reg["resolution_type"] = tipo
            if nota:
                reg["nota"] = nota
            datos.setdefault(destino, []).append(reg)
            encontrados.add(str(p.get("display_id")))
            movidos += 1
        else:
            pend.append(p)
    datos["pending"] = pend
    resueltos = []
    for r in datos.get("resolved", []):
        if isinstance(r, dict) and r.get("resolution_type") == "mostrado" and _cu_coincide(r, objetivo):
            encontrados.add(str(r.get("display_id")))
            movidos += 1
            if r.get("message_id"):
                mids.append(r["message_id"])
            if destino == "resolved":
                r["resolution_type"] = tipo
                r[sello] = now_s
                if nota:
                    r["nota"] = nota
                resueltos.append(r)
            else:
                reg = dict(r)
                reg[sello] = now_s
                reg["resolution_type"] = tipo
                if nota:
                    reg["nota"] = nota
                datos.setdefault(destino, []).append(reg)
            continue
        resueltos.append(r)
    datos["resolved"] = resueltos
    ya_estaban = sorted(
        {
            str(r.get("display_id"))
            for seccion in ("resolved", "discarded")
            for r in datos.get(seccion, [])
            if isinstance(r, dict)
            and _cu_coincide(r, objetivo)
            and str(r.get("display_id")) not in encontrados
            and (seccion == destino or r.get("resolution_type") != "mostrado")
        }
    )
    _cu_purgar(datos, ahora)
    archivo_kb = _cu_escribir(datos)
    salida = {
        "success": True,
        "movidos": movidos,
        "destino": destino,
        "totales": _cu_totales(pend),
        "archivo_kb": archivo_kb,
        "_message_ids": mids,
    }
    if ya_estaban:
        salida["ya_estaban_en_ese_estado"] = ya_estaban
    if movidos == 0 and not ya_estaban:
        salida["success"] = False
        salida["error"] = (
            "No se movió ningún correo: esos #ID o ids no están en la lista guardada. NO digas que los descartaste ni "
            "los resolviste; confirma los #ID con el usuario o con accion=resolver_id."
        )
    elif movidos == 0:
        salida["aviso"] = "Ya estaban en ese estado; no hubo cambios."
    return salida


_CU_NO_DISPONIBLE = ("mensaje_no_autorizado", "mensaje_fuera_de_carpeta", "404", "itemnotfound", "no encontrado")


def _cu_referencia(args):
    tarea = _current_task_id()
    if not tarea:
        return {"success": False, "error": "Esta acción solo funciona dentro de una conversación activa."}
    pedido = {"task_id": tarea}
    for clave in ("referencia", "ejecucion", "display_id", "remitente"):
        valor = args.get(clave)
        if valor not in (None, ""):
            pedido[clave] = str(valor)[:300]
    if args.get("posicion") not in (None, ""):
        try:
            pedido["posicion"] = int(args.get("posicion"))
        except (TypeError, ValueError):
            pass
    respuesta = _llamar_control("POST", "/agents/self/correo/referencia", pedido)
    if respuesta.get("ok") is False:
        return {"success": False, "error": f"No pude consultar los resúmenes: {respuesta.get('error')}"}
    respuesta.pop("_http", None)
    if respuesta.get("estado") != "resuelto":
        return respuesta
    correo = dict(respuesta.get("correo") or {})
    mid = str(correo.get("message_id") or "")
    if not mid:
        return {
            "success": False,
            "estado": "sin_identificador",
            "ejecucion": respuesta.get("ejecucion"),
            "correo": correo,
            "pregunta": (
                f"El correo {correo.get('display_id')} «{correo.get('asunto')}» está en el resumen, pero no tengo su "
                "identificador verificado. Dile al usuario que no puedo abrirlo con certeza y ofrece buscarlo por "
                "asunto con ms365_mail_search; no adivines."
            ),
        }
    user_id = _task_user_id()
    token, err = _broker_token_check(user_id or "")
    if not token:
        return {"success": False, "error": f"No se pudo leer el correo: {err}"}
    mensaje, error = _fetch_message(token, mid, "text", _GET_MANY_DEFAULT_CHARS)
    if error:
        bajo = str(error).lower()
        if any(marca in bajo for marca in _CU_NO_DISPONIBLE):
            return {
                "success": False,
                "estado": "no_disponible",
                "eliminado_o_movido": True,
                "ejecucion": respuesta.get("ejecucion"),
                "correo": {k: v for k, v in correo.items() if k != "message_id"},
                "pregunta": (
                    f"El correo {correo.get('display_id')} «{correo.get('asunto')}» de {correo.get('remitente')} ya "
                    "no está en la bandeja: fue eliminado o movido a otra carpeta. Díselo al usuario tal cual y no "
                    "prepares ninguna acción sobre él."
                ),
            }
        return {"success": False, "error": f"No pude leer el correo {correo.get('display_id')}: {error}"}
    _remember_ids([mensaje])
    remitente = ((mensaje.get("from") or {}).get("emailAddress") or {}) if isinstance(mensaje, dict) else {}
    return {
        "success": True,
        "estado": "resuelto",
        "ejecucion": respuesta.get("ejecucion"),
        "correo": {
            **correo,
            "message_id": mensaje.get("id") or mid,
            "asunto_actual": mensaje.get("subject"),
            "de": remitente.get("address"),
            "recibido": mensaje.get("receivedDateTime"),
            "cuerpo": (mensaje.get("body") or {}).get("content"),
        },
        "siguiente": respuesta.get("siguiente")
        or (
            "Es el correo exacto del resumen. Para responderlo o reenviarlo usa este message_id EXACTO en "
            "ms365_mail_reply o ms365_mail_forward (vista previa y aprobación del usuario antes de enviar)."
        ),
    }


def _correo_sync_tool(args, **_kwargs):
    accion = (args or {}).get("accion") or "ventana"
    try:
        if accion == "ventana":
            return json.dumps(_cu_ventana(args or {}), ensure_ascii=False)
        if accion == "plan":
            return json.dumps(_cu_plan(args or {}), ensure_ascii=False)
        if accion == "commit":
            return json.dumps(_cu_commit(args or {}), ensure_ascii=False)
        if accion == "resolver_id":
            return json.dumps(_cu_resolver_id(args or {}), ensure_ascii=False)
        if accion == "resolver":
            return json.dumps(_cu_resolver(args or {}), ensure_ascii=False)
        if accion == "preferencias":
            return json.dumps(_cu_preferencias(args or {}), ensure_ascii=False)
        if accion == "marcar_leidos":
            return json.dumps(_cu_marcar_leidos_ahora(args or {}), ensure_ascii=False)
        if accion == "referencia":
            return json.dumps(_cu_referencia(args or {}), ensure_ascii=False)
        return json.dumps({"success": False, "error": f"acción desconocida: {accion}"}, ensure_ascii=False)
    except EstadoOcupado as exc:
        return json.dumps(
            {"success": False, "ocupado": True, "error": f"ms365_correo_sync({accion}): {exc}"}, ensure_ascii=False
        )
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"success": False, "error": f"ms365_correo_sync({accion}): {exc}"}, ensure_ascii=False)


def register(ctx):
    ctx.register_tool(
        name="ms365_mail_list",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_list",
            "description": (
                "Lista los correos del usuario desde Microsoft 365. Devuelve asunto, remitente, fecha y preview del "
                "cuerpo. Para clasificar la bandeja o revisar pendientes usa unread_only=true: los correos ya leídos "
                "o respondidos no son pendientes."
            ),
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
                            "Solo correos recibidos desde esta fecha/hora. Pásala en hora de Chile SIN zona "
                            "(ej. 2026-10-06T17:00:00 = 17:00 en Chile); no la conviertas a UTC. "
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
                    "name": {"type": "string", "description": "Nombre o apellido a buscar (ej. 'Rosa Lagos')"},
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Varias personas en UNA llamada (hasta 5), en vez de llamar la herramienta una por una.",
                    },
                    "max_results": {"type": "integer", "description": "Máximo de candidatos (default 8, máx 20)"},
                },
                "required": [],
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
                "RESPONDE un correo recibido (al remitente o a todos) dentro del mismo hilo. USA ESTA, no "
                "ms365_mail_send, para responder: los destinatarios se toman del correo original, así que NO los "
                "escribes a mano ni los puedes equivocar. Para una respuesta simple usa comment (texto citado "
                "debajo). Para responder CON FIRMA o imágenes, pasa body (HTML) e inline_images igual que en "
                "ms365_mail_send: se envía como respuesta del hilo, con tu contenido arriba y el original citado "
                "debajo. La primera llamada devuelve una vista previa sin enviar; solo con "
                "aprobado_por_el_usuario=true, en un mensaje posterior del usuario, se envía."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "ID del correo a responder."},
                    "comment": {"type": "string", "description": "Texto simple de la respuesta (citado debajo)."},
                    "body": {
                        "type": "string",
                        "description": "Cuerpo HTML de la respuesta (para incluir la firma). Alternativa a comment.",
                    },
                    "inline_images": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Imágenes en línea (firma), mismo formato que ms365_mail_send: [{content_id, path}].",
                    },
                    "firma": {
                        "type": "string",
                        "description": (
                            "Forma de incluir la firma: pasa el NOMBRE de la firma (p. ej. 'personal'); el servidor "
                            "agrega la firma COMPLETA (imágenes + bloque HTML con sus cid:) desde work/firma/manifest.json. "
                            "NO escribas la firma ni los cid: en el cuerpo: redacta solo el mensaje (termina en 'Saludos,') "
                            "y el servidor pone la firma. Si el cuerpo ya trae los cid: de la firma, no se duplica."
                        ),
                    },
                    "reply_all": {
                        "type": "boolean",
                        "description": "true: responder a todos (mantiene a los de copia).",
                    },
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true SOLO si el usuario ya vio la vista previa y dijo que sí.",
                    },
                },
                "required": ["message_id"],
            },
        },
        handler=_reply_email_tool,
        description="Responder un correo recibido (con aprobación)",
        emoji="↩️",
    )
    ctx.register_tool(
        name="ms365_mail_mark_read",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_mark_read",
            "description": (
                "Marca uno o VARIOS correos como leído (o no leído con is_read=false) en Microsoft 365. "
                "Para marcar varios usa message_ids con la lista completa en UNA sola llamada; no la repitas por cada correo. "
                "PREGUNTA SIEMPRE al usuario antes de marcar y hazlo solo si confirma; NO marques por tu cuenta al "
                "leer un correo con ms365_mail_get ni al responder con ms365_mail_reply. Usa los ids EXACTOS de "
                "ms365_mail_list o ms365_mail_search. Solo se marcan correos que hayas listado o leído en este buzón; "
                "un id que no venga de una lista/lectura se rechaza (no inventes ni marques ids de memoria)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IDs de los correos a marcar (de ms365_mail_list o ms365_mail_search), todos en una llamada.",
                    },
                    "message_id": {
                        "type": "string",
                        "description": "ID de un solo correo (alternativa a message_ids si es uno solo).",
                    },
                    "is_read": {
                        "type": "boolean",
                        "description": "true (default) marca como leído; false lo deja como no leído.",
                    },
                },
            },
        },
        handler=_mark_read_tool,
        description="Marcar un correo como leído/no leído (con confirmación del usuario)",
        emoji="✅",
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
                "(to/cc/bcc) y archivos adjuntos (attachments). FLUJO OBLIGATORIO, siempre igual: (1) para mostrar "
                "un borrador LLAMA a esta herramienta sin aprobado_por_el_usuario: devuelve el campo 'borrador', que "
                "copias TAL CUAL al usuario, y terminas con «¿Lo envío?»; nunca escribas un borrador a mano. (2) Si "
                "el usuario pide cambios, vuelve al paso 1 con el cambio. (3) Cuando diga que sí (envía, sí, ok), "
                "llama de nuevo con aprobado_por_el_usuario=true y los mismos destinatarios, asunto y adjuntos; "
                "nunca digas que se envió sin success=true. «Envíamelo solo a mí / pruébalo conmigo para validar»: usa ESTA "
                "herramienta con to=['yo'] (se envía al propio usuario, con firma y asunto «RE: …» permitido), no un "
                "borrador. Si el usuario pide solo un borrador («no lo envíes»), usa "
                "ms365_mail_create_draft. La firma personal la agrega SIEMPRE el servidor "
                "(firma='<nombre>' para otra firma de work/firma/manifest.json, firma='ninguna' solo si el usuario lo pide); "
                "no la escribas "
                "en el cuerpo ni es motivo para no enviar. "
                "Para firmas o imágenes "
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
                            "varias direcciones separadas por ; o ,). Admite tantos como necesites. Para el propio "
                            "usuario («a mí mismo») usa 'yo'. Nunca inventes una dirección."
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
                    "firma": {
                        "type": "string",
                        "description": (
                            "Forma de incluir la firma: pasa el NOMBRE de la firma (p. ej. 'personal'); el servidor "
                            "agrega la firma COMPLETA (imágenes + bloque HTML con sus cid:) desde work/firma/manifest.json. "
                            "NO escribas la firma ni los cid: en el cuerpo: redacta solo el mensaje (termina en 'Saludos,') "
                            "y el servidor pone la firma. Si el cuerpo ya trae los cid: de la firma, no se duplica."
                        ),
                    },
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true SOLO si el usuario ya vio la vista previa y dijo que sí.",
                    },
                },
                "required": ["to", "subject", "body"],
            },
        },
        handler=_send_email_tool,
        description="Enviar correo M365 en nombre del usuario (con aprobación)",
        emoji="📤",
    )
    ctx.register_tool(
        name="ms365_mail_create_draft",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_create_draft",
            "description": (
                "Guarda un correo como BORRADOR en la carpeta Borradores de Outlook del usuario, SIN enviarlo y sin "
                "pedir aprobación. Úsala SOLO cuando el usuario pida explícitamente un borrador, «déjalo como borrador» "
                "o «no lo envíes». NUNCA la uses para mostrar una vista previa antes de enviar (eso lo hace "
                "ms365_mail_send sin aprobado_por_el_usuario): crea un correo real en su Outlook. "
                "Mismos argumentos y misma firma que ms365_mail_send. Responde con lo que devuelve la herramienta "
                "(success=true y estado=borrador_guardado); nunca digas que quedó guardado sin ese resultado, y no "
                "digas que se envió."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Destinatarios, una dirección por elemento; 'yo' para el propio usuario.",
                    },
                    "subject": {"type": "string", "description": "Asunto del correo"},
                    "body": {"type": "string", "description": "Cuerpo del correo (HTML o texto plano)"},
                    "cc": {"type": "array", "items": {"type": "string"}, "description": "Copia (opcional)"},
                    "bcc": {"type": "array", "items": {"type": "string"}, "description": "Copia oculta (opcional)"},
                    "attachments": {
                        "type": "array",
                        "description": "Archivos adjuntos del directorio de trabajo (máx. 10, 25 MB en total).",
                        "items": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}, "name": {"type": "string"}},
                            "required": ["path"],
                        },
                    },
                    "inline_images": {
                        "type": "array",
                        "description": "Imágenes incrustadas: [{path, content_id}].",
                        "items": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}, "content_id": {"type": "string"}},
                            "required": ["path", "content_id"],
                        },
                    },
                    "firma": {
                        "type": "string",
                        "description": (
                            "Nombre de la firma en work/firma/manifest.json ('personal' por defecto) o 'ninguna'; "
                            "la agrega el servidor."
                        ),
                    },
                },
                "required": ["to", "subject", "body"],
            },
        },
        handler=_create_draft_tool,
        description="Guardar un correo M365 como borrador (sin enviar)",
        emoji="📝",
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
        name="ms365_correo_sync",
        toolset=TOOLSET,
        schema={
            "name": "ms365_correo_sync",
            "description": (
                "Motor de estado del cron 'correo unificado' (work/correo_unificado_estado.json): hace TODO el "
                "trabajo mecánico en el servidor (ventana por tarea programada desde su última corrida confirmada, "
                "dedup por internetMessageId, merge peor-caso, escalonamiento por tiempo, consolidar FRÍOS inertes, "
                "display_id estable, resueltos/descartados, cap de tamaño) y devuelve solo digests compactos. TÚ NO lees ni escribes ese archivo ni usas execute_code "
                "sobre él. Flujo del cron: (1) accion='ventana' → te dice desde cuándo y cuántos correos listar; "
                "(2) ms365_mail_list con esos parámetros; (3) accion='plan' pasando la salida cruda de mail_list → "
                "te devuelve SOLO los correos NUEVOS por clasificar (con un token corto N1,N2,…); (4) clasifica SOLO "
                "esos nuevos; (5) accion='commit' con las clasificaciones por token → devuelve el DIGEST final para "
                "armar el informe. En chat: accion='referencia' cuando el usuario se refiere a un correo de un resumen "
                "que le llegó («el segundo», «el #849», «el de Juan»): pasa su frase en referencia y devuelve el "
                "correo exacto o la pregunta de aclaración (no adivines por el texto del resumen); "
                "accion='resolver_id' (display_id→message_id) y accion='resolver' "
                "(mover a resolved/discarded tras responder/descartar)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "accion": {
                        "type": "string",
                        "description": "ventana | plan | commit | referencia | resolver_id | resolver | preferencias | marcar_leidos. referencia: identifica el correo de un resumen de cron que el usuario nombra («el segundo», «el #849», «el de Juan»). preferencias: el usuario elige qué categorías del informe se marcan como leídas desde ahora (marcar_leidos_categorias). marcar_leidos: marca ya como leídos los correos del último informe de esas categorias o display_ids.",
                    },
                    "marcar_leidos_categorias": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'accion=preferencias: categorías que se marcan como leídas en cada informe (INMEDIATO, CRÍTICO, CALIENTE, TIBIO, FRÍO, OTROS). El resto queda sin leer. ["todas"] vuelve al comportamiento normal.',
                    },
                    "categorias": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "accion=marcar_leidos: categorías del último informe a marcar como leídas ya.",
                    },
                    "marcar_leido": {
                        "type": "boolean",
                        "description": "accion=resolver: true si el usuario pidió además marcar esos correos como leídos en Outlook («descarta y marca como leído», «déjalo leído»). Sin esto, descartar solo los saca del informe y siguen sin leer en Outlook.",
                    },
                    "descartar_marca_leido": {
                        "type": "boolean",
                        "description": "accion=preferencias: el usuario dice que descartar implica dejar el correo como leído (regla permanente).",
                    },
                    "mode": {"type": "string", "description": "hourly (default) o daily; solo para accion=ventana"},
                    "mensajes": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "accion=plan (opcional): la salida CRUDA de ms365_mail_list. Si se omite, el servidor lee la bandeja desde 'since'.",
                    },
                    "since": {
                        "type": "string",
                        "description": "accion=plan sin mensajes: ISO UTC desde el que leer la bandeja (default: la ventana de esta tarea). Si es posterior a la última corrida confirmada se amplía hacia atrás para no dejar huecos.",
                    },
                    "ventana_horas": {
                        "type": "number",
                        "description": "accion=ventana/plan: horas máximas hacia atrás (default 168; 48 si la tarea no tiene corrida confirmada).",
                    },
                    "vista_no_leidos_horas": {
                        "type": "number",
                        "description": "accion=ventana/plan: el informe muestra TODOS los no leídos de esas horas (p. ej. 48), también los ya mostrados, hasta que se lean en Outlook. accion=preferencias (solo en el chat, a pedido del usuario): lo deja fijo para sus informes; 0 vuelve a solo lo nuevo.",
                    },
                    "solapamiento_min": {
                        "type": "number",
                        "description": "accion=ventana/plan: minutos de solapamiento antes de la última corrida confirmada (default 10).",
                    },
                    "max_paginas": {
                        "type": "integer",
                        "description": "accion=plan: páginas de 50 correos a leer como máximo (default 10).",
                    },
                    "clasificaciones": {
                        "type": "object",
                        "description": "accion=commit: {token: {display_category, base_temperature, relationship, operational_status, action_type, action_required, requires_response, due_date, due_status, ruido_automatico}} SOLO para los nuevos que devolvió plan. {} si no hay nuevos.",
                    },
                    "display_id": {
                        "type": "string",
                        "description": "accion=resolver_id o referencia: el #ID a resolver.",
                    },
                    "referencia": {
                        "type": "string",
                        "description": "accion=referencia: la frase del usuario tal cual («quiero trabajar con el segundo correo»). Si se omite se usa su último mensaje.",
                    },
                    "ejecucion": {
                        "type": "string",
                        "description": "accion=referencia (opcional): task_id o hora local HH:MM del resumen elegido cuando el usuario aclara de cuál resumen habla.",
                    },
                    "posicion": {
                        "type": "integer",
                        "description": "accion=referencia (opcional): posición en el resumen (1 = primero).",
                    },
                    "remitente": {
                        "type": "string",
                        "description": "accion=referencia (opcional): nombre del remitente.",
                    },
                    "display_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "accion=resolver: #IDs a mover.",
                    },
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "accion=resolver: message_ids a mover.",
                    },
                    "destino": {"type": "string", "description": "accion=resolver: 'resolved' o 'discarded'."},
                    "nota": {"type": "string", "description": "accion=resolver: nota breve opcional."},
                    "marcar_leidos": {
                        "type": "boolean",
                        "description": "accion=commit: por defecto NO marca nada como leído. true solo si el usuario lo pidió en este chat; en una tarea programada se marca únicamente si su instrucción lo declara.",
                    },
                    "verificar_leidos": {
                        "type": "boolean",
                        "description": "accion=commit: por defecto true, auto-resuelve los RESPONDIDOS.",
                    },
                    "ahora": {"type": "string", "description": "ISO UTC opcional (default: ahora)."},
                },
                "required": [],
            },
        },
        handler=_correo_sync_tool,
        description="Motor de estado del cron correo unificado (v5)",
        emoji="📨",
    )
    ctx.register_tool(
        name="ms365_mail_search",
        toolset=TOOLSET,
        schema={
            "name": "ms365_mail_search",
            "description": "Busca correos en el buzón Microsoft 365 del usuario (todo el buzón o una carpeta). Combina en UNA sola llamada palabras (query) con from (nombre o correo), subject, since, until, has_attachments y folder; no repitas la búsqueda variando una palabra. Devuelve los resultados del más reciente al más antiguo.",
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
