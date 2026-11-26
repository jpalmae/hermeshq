from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
BROKER_TOKEN = "__broker__"
TOOLSET = "hermeshq_sharepoint"

_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_DOWNLOAD_DIR = "sharepoint_downloads"
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_DEFAULT_MAX_CHARS = 20_000
_HARD_MAX_CHARS = 100_000
_MAX_PDF_PAGES = 200
_MAX_SHEET_ROWS = 2000
_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".yaml",
    ".yml",
    ".ini",
    ".rtf",
}


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


def _task_id() -> str | None:
    raw = os.environ.get("HERMESHQ_TASK_PAYLOAD", "")
    if raw:
        try:
            return str((json.loads(raw) or {}).get("task_id") or "").strip() or None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _broker_headers() -> tuple[str, dict, str | None]:
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    agent_id = os.environ.get("HERMESHQ_AGENT_ID", "")
    agent_token = os.environ.get("HERMESHQ_AGENT_TOKEN", "")
    headers = {
        "X-HermesHQ-Agent-ID": agent_id,
        "X-HermesHQ-Agent-Token": agent_token,
        "X-HermesHQ-Attempt-ID": os.environ.get("HERMESHQ_ATTEMPT_ID", ""),
        "X-HermesHQ-Lease-Generation": os.environ.get("HERMESHQ_LEASE_GENERATION", ""),
    }
    return base_url, headers, _task_id()


def _get_m365_token(user_id: str) -> tuple[str | None, str | None, str]:
    base_url, headers, tarea = _broker_headers()
    if not base_url or not headers["X-HermesHQ-Agent-ID"] or not headers["X-HermesHQ-Agent-Token"]:
        return None, None, "HermesHQ internal control no configurado"
    if not tarea:
        return None, None, "No hay tarea activa para autorizar el acceso a SharePoint."
    url = f"{base_url}/control/m365/contexto?tarea_id={urllib.parse.quote(tarea)}"
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            site_url = (data.get("sharepoint_site_url") or "").strip().rstrip("/") or None
            return BROKER_TOKEN, site_url, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail", body)
        except (json.JSONDecodeError, ValueError, AttributeError):
            detail = body
        return None, None, str(detail)
    except Exception as exc:  # noqa: BLE001  # HTTP request catch-all
        return None, None, str(exc)


_MOTIVOS_NO_ENCONTRADO = {"item_no_resuelto", "unidad_no_identificada", "identificador_de_mensaje_no_valido"}


def _error_de_politica(motivo) -> dict:
    motivo = str(motivo or "").strip()
    if motivo in _MOTIVOS_NO_ENCONTRADO:
        return {
            "error": (
                f"No pude abrir ese archivo ({motivo}): no existe con ese id o la cuenta no tiene acceso a él. "
                "No es una restricción de datos de HermesHQ. Usa exactamente el item_id y el drive_id que devolvió "
                "sharepoint_search para ese archivo; si ya lo hiciste, búscalo de nuevo por su nombre."
            ),
            "_graph_code": "not_found",
        }
    return {
        "error": (
            f"Acceso denegado por la política de datos de este agente ({motivo}): esa ubicación u operación no "
            "está habilitada para él. Díselo así al usuario; el administrador puede ampliar el alcance."
        ),
        "_graph_code": "policy_denied",
    }


def _broker_request(
    metodo: str,
    ruta: str,
    cuerpo_json: dict | None = None,
    cuerpo_bytes: bytes | None = None,
    confirmacion_compartir: str | None = None,
) -> dict:
    base_url, headers, tarea = _broker_headers()
    body: dict = {"tarea_id": tarea, "metodo": metodo.upper(), "ruta": ruta}
    if confirmacion_compartir:
        body["confirmacion_compartir"] = confirmacion_compartir
    if cuerpo_json is not None:
        body["cuerpo_json"] = cuerpo_json
    if cuerpo_bytes is not None:
        import base64 as _b64

        body["cuerpo_b64"] = _b64.b64encode(cuerpo_bytes).decode("ascii")
    req = urllib.request.Request(
        f"{base_url}/control/m365/graph",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detalle = json.loads(raw).get("detail", raw)
        except (json.JSONDecodeError, ValueError, AttributeError):
            detalle = raw
        motivo = detalle.get("motivo") if isinstance(detalle, dict) else str(detalle)
        return _error_de_politica(motivo)
    except Exception as exc:  # noqa: BLE001  # HTTP request catch-all
        return {"error": f"No se pudo consultar SharePoint a través de HermesHQ: {exc}"}


def _ruta_graph(url: str) -> str:
    return url[len(GRAPH_BASE) :] if url.startswith(GRAPH_BASE) else url


def _encode_url(url: str) -> str:
    return urllib.parse.quote(url, safe="%:/?#[]@!$&'()*+,;=~")


def _graph(method: str, path: str, access_token: str, payload: dict | None = None) -> dict:
    if access_token == BROKER_TOKEN:
        res = _broker_request(method, _ruta_graph(path), cuerpo_json=payload)
        if "error" in res:
            return res
        return res.get("json") or {}
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
            "itemNotFound": "Archivo o carpeta no encontrado (404).",
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


_LOOKUP_HINT = (
    " Pista: no adivines rutas, nombres de sitio ni ids, y no reintentes la misma ruta. Busca primero con "
    "sharepoint_search (palabras del nombre) y usa el item_id y drive_id del resultado: para leer o descargar un "
    "archivo pásalos a sharepoint_read_file o sharepoint_download_file, y para listar una carpeta pásalos a "
    "sharepoint_list_files o sharepoint_download_folder. Para ver los sitios y bibliotecas disponibles usa "
    "sharepoint_list_drives."
)
_LOOKUP_ERROR_MARKERS = (
    "404",
    "root:",
    "Url specified is invalid",
    "Invalid request",
    "no encontrado",
    "itemNotFound",
    "could not be found",
)


def _with_hint(error: str) -> str:
    text = str(error)
    if any(marker in text for marker in _LOOKUP_ERROR_MARKERS) and "Pista:" not in text:
        return text + _LOOKUP_HINT
    return text


_MAX_CANDIDATES = 5


def _buscar_unidades(token: str, payload: dict) -> dict:
    req = (payload.get("requests") or [{}])[0]
    consulta = str((req.get("query") or {}).get("queryString") or "").strip()
    tamano = int(req.get("size") or 10)
    if not consulta:
        return {"error": "Se requiere una consulta."}
    global_ = _graph("POST", "/search/query", token, payload)
    if "error" not in global_ or global_.get("_graph_code") != "policy_denied":
        return global_
    drives = _graph("GET", "/me/drives?$select=id,name", token)
    if "error" in drives:
        return drives
    hits: list = []
    consulta_graph = consulta.replace("'", "''")
    for unidad in drives.get("value", []):
        res = _graph(
            "GET",
            f"/drives/{unidad.get('id')}/root/search(q='{consulta_graph}')?$select=id,name,webUrl,lastModifiedDateTime,size,parentReference",
            token,
        )
        if "error" in res:
            return res
        for item in res.get("value", []):
            hits.append({"resource": item})
            if len(hits) >= tamano:
                break
        if len(hits) >= tamano:
            break
    return {"value": [{"hitsContainers": [{"hits": hits}]}]}


def _search_candidates(token: str, path_text: str, limit: int = _MAX_CANDIDATES) -> list[dict]:
    last = str(path_text or "").strip("/").split("/")[-1].strip()
    name = os.path.splitext(last)[0] or last
    if len(name) < 3:
        return []
    payload = {
        "requests": [
            {
                "entityTypes": ["driveItem"],
                "query": {"queryString": name},
                "from": 0,
                "size": limit,
                "fields": ["id", "name", "webUrl", "parentReference"],
            }
        ]
    }
    result = _buscar_unidades(token, payload)
    if "error" in result:
        return []
    found = []
    for resp in result.get("value", []):
        for container in resp.get("hitsContainers", []):
            for hit in container.get("hits", []):
                resource = hit.get("resource", {})
                parent = resource.get("parentReference") or {}
                found.append(
                    {
                        "name": resource.get("name"),
                        "folder": str(parent.get("path") or "").split("root:", 1)[-1].lstrip("/"),
                        "item_id": resource.get("id"),
                        "drive_id": parent.get("driveId"),
                        "url": resource.get("webUrl"),
                    }
                )
    return found[:limit]


def _failure_with_candidates(error, path_text: str, token: str) -> str:
    answer: dict = {"success": False, "error": _with_hint(error)}
    if any(marker in str(error) for marker in _LOOKUP_ERROR_MARKERS):
        candidates = _search_candidates(token, path_text)
        if candidates:
            answer["candidatos"] = candidates
            answer["siguiente"] = (
                "La ruta no existe tal cual; NO la reintentes y no pruebes más rutas a mano. Toma el item_id y "
                "drive_id de uno de estos 'candidatos' y pásalos a la herramienta: para listar una carpeta usa "
                "sharepoint_list_files o sharepoint_download_folder, y para un archivo sharepoint_read_file o "
                "sharepoint_download_file."
            )
    return json.dumps(answer, ensure_ascii=False)


_MAX_TREE_ITEMS = 300


def _fetch_children(base: str, folder: str, token: str) -> tuple[list | None, str | None]:
    path: str | None = f"{base}:/{folder}:/children" if folder else f"{base}/children"
    items: list = []
    while path and len(items) < 200:
        result = _graph("GET", path, token)
        if "error" in result:
            return None, result["error"]
        items.extend(result.get("value", []))
        next_link: str = result.get("@odata.nextLink") or ""
        path = next_link if (next_link and len(items) < 200) else None
    return items, None


def _site_bases(site_url: str, folder_path: str, token: str) -> list[tuple[str, str, str | None]]:
    """Candidatos (base, carpeta, nombre de biblioteca): prueba la biblioteca nombrada en la ruta y luego todas las del sitio."""
    parsed = urllib.parse.urlparse(site_url)
    hostname = parsed.netloc
    site_path = parsed.path.rstrip("/") or "/"
    default = (f"/sites/{hostname}:{site_path}:/drive/root", folder_path, None)
    site = _graph("GET", f"/sites/{hostname}:{site_path}?$select=id", token)
    if "error" in site or not site.get("id"):
        return [default]
    drives = _graph("GET", f"/sites/{site['id']}/drives?$select=id,name", token).get("value", [])
    partes = [p for p in folder_path.split("/") if p]
    candidatos: list[tuple[str, str, str | None]] = []
    if partes:
        for drive in drives:
            if _fold_name(drive.get("name")) == _fold_name(partes[0]):
                candidatos.append((f"/drives/{drive['id']}/root", "/".join(partes[1:]), drive.get("name")))
    candidatos.append(default)
    for drive in drives:
        candidatos.append((f"/drives/{drive['id']}/root", folder_path, drive.get("name")))
    return candidatos


def _fold_name(value) -> str:
    import unicodedata

    texto = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", texto.lower())


def _simplify_item(i: dict) -> dict:
    return {
        "id": i.get("id"),
        "name": i.get("name"),
        "type": "folder" if "folder" in i else "file",
        "size": i.get("size"),
        "url": i.get("webUrl"),
        "modified": i.get("lastModifiedDateTime"),
    }


def _build_tree(base: str, folder: str, token: str, depth: int, budget: list[int]) -> list[dict]:
    items, error = _fetch_children(base, folder, token)
    if items is None:
        return [{"name": f"(no se pudo leer: {str(error)[:60]})", "type": "error"}]
    nodes: list[dict] = []
    for item in items:
        if budget[0] <= 0:
            nodes.append({"name": "… (se omitió el resto por tamaño)", "type": "nota"})
            break
        budget[0] -= 1
        node = {"name": item.get("name"), "type": "folder" if "folder" in item else "file"}
        if node["type"] == "file":
            node["size"] = item.get("size")
        elif depth > 1:
            sub = f"{folder}/{item.get('name')}" if folder else str(item.get("name"))
            node["children"] = _build_tree(base, sub, token, depth - 1, budget)
        nodes.append(node)
    return nodes


def _build_folder_tree(
    base: str, folder: str, token: str, depth: int, budget: list[int]
) -> tuple[list[dict], int | None]:
    items, error = _fetch_children(base, folder, token)
    if items is None:
        return [{"name": f"(no se pudo leer: {str(error)[:60]})", "type": "error"}], None
    nodes: list[dict] = []
    for item in items:
        if "folder" not in item:
            continue
        if budget[0] <= 0:
            nodes.append(
                {
                    "name": "… (se omitieron más carpetas por tamaño; pide una subcarpeta o sube max_items)",
                    "type": "nota",
                }
            )
            break
        budget[0] -= 1
        node = {"name": item.get("name"), "type": "folder"}
        if depth > 1:
            sub = f"{folder}/{item.get('name')}" if folder else str(item.get("name"))
            node["children"], archivos = _build_folder_tree(base, sub, token, depth - 1, budget)
            if archivos is not None:
                node["archivos"] = archivos
        nodes.append(node)
    return nodes, sum(1 for i in items if "folder" not in i)


def _list_files_tool(args: dict, **_kwargs) -> str:
    """Lista archivos con Files.Read.All en OneDrive o SharePoint; depth 2-3 devuelve el árbol de subcarpetas en una sola llamada."""
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, user_site_url, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)

    site_url = str(args.get("site_url") or user_site_url or "").strip().rstrip("/")
    folder_path = str(args.get("folder_path") or "").strip().strip("/")
    item_id = str(args.get("item_id") or "").strip()
    drive_id = str(args.get("drive_id") or "").strip()
    solo_carpetas = bool(args.get("solo_carpetas"))
    try:
        depth = max(1, min(4 if solo_carpetas else 3, int(args.get("depth") or 1)))
    except (TypeError, ValueError):
        depth = 1
    try:
        max_items = max(50, min(800, int(args.get("max_items") or _MAX_TREE_ITEMS)))
    except (TypeError, ValueError):
        max_items = _MAX_TREE_ITEMS

    if item_id:
        item_base = f"/drives/{drive_id}/items/{item_id}" if drive_id else f"/me/drive/items/{item_id}"
        candidatos = [(item_base, "", None)]
    else:
        if not site_url and folder_path:
            folder_path, folder_error = _normalize_drive_folder(folder_path)
            if folder_error:
                return json.dumps({"success": False, "error": folder_error}, ensure_ascii=False)
        candidatos = _site_bases(site_url, folder_path, token) if site_url else [("/me/drive/root", folder_path, None)]
    raw_items: list | None = None
    last_error = "No se pudo listar la carpeta."
    usado: tuple[str, str, str | None] | None = None
    for base, carpeta, nombre in candidatos:
        raw_items, error = _fetch_children(base, carpeta, token)
        if raw_items is not None:
            usado = (base, carpeta, nombre)
            break
        last_error = error or last_error
    if raw_items is None or usado is None:
        return _failure_with_candidates(last_error, folder_path, token)
    base, carpeta, nombre = usado
    resultado: dict = {"success": True}
    if nombre:
        resultado["biblioteca"] = nombre
    if depth > 1:
        presupuesto = [max_items]
        if solo_carpetas:
            arbol, archivos_raiz = _build_folder_tree(base, carpeta, token, depth, presupuesto)
            resultado["tree"] = arbol
            resultado["archivos_en_la_raiz"] = archivos_raiz
        else:
            resultado["tree"] = _build_tree(base, carpeta, token, depth, presupuesto)
        resultado["count"] = max_items - presupuesto[0]
        return json.dumps(resultado, ensure_ascii=False)
    simplified = [_simplify_item(i) for i in raw_items]
    resultado.update({"count": len(simplified), "items": simplified})
    return json.dumps(resultado, ensure_ascii=False)


def _get_file_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, user_site_url, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)

    file_path = str(args.get("file_path") or "").strip().strip("/")
    site_url = str(args.get("site_url") or user_site_url or "").strip().rstrip("/")

    if not file_path:
        return json.dumps({"success": False, "error": "Se requiere file_path."})

    if site_url:
        parsed = urllib.parse.urlparse(site_url)
        hostname = parsed.netloc
        site_path = parsed.path.rstrip("/") or "/"
        path = f"/sites/{hostname}:{site_path}:/drive/root:/{file_path}"
    else:
        path = f"/me/drive/root:/{file_path}"

    result = _graph("GET", path, token)
    if "error" in result:
        return _failure_with_candidates(result["error"], file_path, token)
    return json.dumps({"success": True, "item": result}, ensure_ascii=False)


def _list_drives_tool(args: dict, **_kwargs) -> str:
    """List accessible drives (OneDrive + SharePoint document libraries)."""
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, _, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)

    path: str | None = "/me/drives"
    raw_drives: list = []
    while path and len(raw_drives) < 200:
        result = _graph("GET", path, token)
        if "error" in result:
            return json.dumps({"success": False, "error": result["error"]})
        raw_drives.extend(result.get("value", []))
        next_link: str = result.get("@odata.nextLink") or ""
        path = next_link if (next_link and len(raw_drives) < 200) else None

    simplified = [
        {"id": d.get("id"), "name": d.get("name"), "type": d.get("driveType"), "url": d.get("webUrl")}
        for d in raw_drives
    ]
    return json.dumps({"success": True, "count": len(simplified), "drives": simplified}, ensure_ascii=False)


def _search_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, _, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)

    query = str(args.get("query") or "").strip()
    if not query:
        return json.dumps({"success": False, "error": "Se requiere query."})
    count = min(int(args.get("count") or 10), 25)

    payload = {
        "requests": [
            {
                "entityTypes": ["driveItem"],
                "query": {"queryString": query},
                "from": 0,
                "size": count,
                "fields": ["id", "name", "webUrl", "lastModifiedDateTime", "size", "parentReference"],
            }
        ]
    }
    result = _buscar_unidades(token, payload)
    if "error" in result:
        return json.dumps({"success": False, "error": result["error"]})
    hits = []
    for resp in result.get("value", []):
        for hc in resp.get("hitsContainers", []):
            for hit in hc.get("hits", []):
                resource = hit.get("resource", {})
                hits.append(
                    {
                        "name": resource.get("name"),
                        "url": resource.get("webUrl"),
                        "modified": resource.get("lastModifiedDateTime"),
                        "size": resource.get("size"),
                        "item_id": resource.get("id"),
                        "drive_id": (resource.get("parentReference") or {}).get("driveId"),
                    }
                )
    return json.dumps({"success": True, "count": len(hits), "hits": hits}, ensure_ascii=False)


# ── Plugin registration ───────────────────────────────────────────────────────


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _download_bytes(url: str, access_token: str | None = None) -> tuple[bytes | None, str]:
    if access_token == BROKER_TOKEN:
        if not url.startswith(GRAPH_BASE):
            return None, "Descarga directa no permitida: el acceso a SharePoint pasa por HermesHQ."
        res = _broker_request("GET", _ruta_graph(url))
        if "error" in res:
            return None, res["error"]
        estado = int(res.get("status") or 0)
        if not 200 <= estado < 300 or "base64" not in res:
            return None, f"Error HTTP {estado} al descargar el archivo."
        import base64 as _b64

        data = _b64.b64decode(res.get("base64", ""))
        if len(data) > _MAX_DOWNLOAD_BYTES:
            return None, f"El archivo supera el límite de {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB."
        return data, ""
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    req = urllib.request.Request(_encode_url(url), headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        try:
            resp = opener.open(req, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                resp = urllib.request.urlopen(exc.headers["Location"], timeout=30)
            else:
                return None, f"Error HTTP {exc.code} al descargar el archivo."
        with resp:
            data = resp.read(_MAX_DOWNLOAD_BYTES + 1)
    except Exception as exc:  # noqa: BLE001  # HTTP request catch-all
        return None, f"No se pudo descargar el archivo: {exc}"
    if len(data) > _MAX_DOWNLOAD_BYTES:
        return None, f"El archivo supera el límite de {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB."
    return data, ""


def _extract_docx(data: bytes, limit: int) -> str:
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts: list[str] = []
    total = 0
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)
            total += len(para.text)
        if total > limit:
            return "\n".join(parts)
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
            total += len(parts[-1])
            if total > limit:
                return "\n".join(parts)
    return "\n".join(parts)


def _extract_xlsx(data: bytes, limit: int) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts: list[str] = []
    total = 0
    for ws in wb.worksheets:
        parts.append(f"## Hoja: {ws.title}")
        for index, row in enumerate(ws.iter_rows(values_only=True)):
            if index >= _MAX_SHEET_ROWS or total > limit:
                break
            line = " | ".join("" if v is None else str(v) for v in row).rstrip(" |")
            if line:
                parts.append(line)
                total += len(line)
        if total > limit:
            break
    return "\n".join(parts)


def _extract_pptx(data: bytes, limit: int) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    parts: list[str] = []
    total = 0
    for number, slide in enumerate(prs.slides, start=1):
        parts.append(f"## Diapositiva {number}")
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text)
                total += len(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.append(" | ".join(cell.text.strip() for cell in row.cells))
                    total += len(parts[-1])
        if total > limit:
            break
    return "\n".join(parts)


def _extract_pdf(data: bytes, limit: int) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    total_pages = len(reader.pages)
    parts: list[str] = []
    total = 0
    for number, page in enumerate(reader.pages[:_MAX_PDF_PAGES], start=1):
        text = (page.extract_text() or "").strip()
        if text:
            parts.append(f"--- Página {number} ---\n{text}")
            total += len(text)
        if total > limit:
            break
    return "\n".join(parts), total_pages


_LEGACY_FORMATS = {
    ".doc": ".docx",
    ".rtf": ".docx",
    ".odt": ".docx",
    ".xls": ".xlsx",
    ".xlsb": ".xlsx",
    ".ods": ".xlsx",
    ".ppt": ".pptx",
    ".odp": ".pptx",
}


def _convert_legacy(name: str, data: bytes) -> tuple[str, bytes] | str:
    ext = os.path.splitext(name.lower())[1]
    target = _LEGACY_FORMATS[ext]
    office = shutil.which("soffice") or shutil.which("libreoffice")
    if not office:
        return f"No hay conversor instalado para leer archivos {ext}."
    with tempfile.TemporaryDirectory() as folder:
        source = os.path.join(folder, "origen" + ext)
        with open(source, "wb") as handle:
            handle.write(data)
        try:
            subprocess.run(
                [office, "--headless", "--convert-to", target.lstrip("."), "--outdir", folder, source],
                capture_output=True,
                timeout=120,
                env=dict(os.environ, HOME=folder),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"La conversión del archivo {ext} tardó más de 2 minutos."
        converted = os.path.join(folder, "origen" + target)
        if not os.path.isfile(converted):
            return f"No se pudo convertir el archivo {ext} para leerlo (¿está dañado o protegido con contraseña?)."
        with open(converted, "rb") as handle:
            return os.path.splitext(name)[0] + target, handle.read()


def _extract_text(name: str, data: bytes, limit: int) -> tuple[str | None, str, dict]:
    ext = os.path.splitext(name.lower())[1]
    extra: dict = {}
    try:
        if ext in _TEXT_EXTENSIONS:
            return data.decode("utf-8", errors="replace"), "", extra
        if ext == ".docx":
            return _extract_docx(data, limit), "", extra
        if ext in (".xlsx", ".xlsm"):
            return _extract_xlsx(data, limit), "", extra
        if ext == ".pptx":
            return _extract_pptx(data, limit), "", extra
        if ext == ".pdf":
            text, pages = _extract_pdf(data, limit)
            extra["pages"] = pages
            if not text.strip():
                return (
                    None,
                    (
                        "El PDF no contiene texto extraíble: es un escaneo o imágenes. Descárgalo con "
                        "sharepoint_download_file y léelo con hq_leer_documento, que convierte sus páginas en imágenes "
                        "para leerlas con vision_analyze. No uses otro archivo como fuente sin decírselo al usuario."
                    ),
                    extra,
                )
            return text, "", extra
    except ImportError as exc:
        return None, f"Falta una librería para leer archivos {ext}: {exc}", extra
    except Exception as exc:  # noqa: BLE001  # parser catch-all
        return None, f"No se pudo leer el contenido del archivo {ext}: {exc}", extra
    if ext in _LEGACY_FORMATS:
        converted = _convert_legacy(name, data)
        if isinstance(converted, str):
            return None, converted, extra
        text, error, extra = _extract_text(converted[0], converted[1], limit)
        extra["convertido_desde"] = ext
        return text, error, extra
    return None, f"Formato {ext or 'desconocido'} no soportado para lectura de contenido.", extra


def _save_to_workspace(name: str, data: bytes) -> str:
    safe = re.sub(r"[^\w.\- ]", "_", os.path.basename(name)).strip(" .") or "archivo"
    target_dir = Path(os.getcwd()) / _DOWNLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
    dest.write_bytes(data)
    _anotar_descarga(dest, data)
    return f"{_DOWNLOAD_DIR}/{dest.name}"


def _ruta_en_espacio_de_subida(raw_path: str) -> Path | None:
    try:
        base = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()).resolve()
        ruta = (base / raw_path).resolve() if not Path(raw_path).is_absolute() else Path(raw_path).resolve()
        return ruta if ruta.is_file() else None
    except (OSError, ValueError):
        return None


def _manifiesto_descargas(carpeta: Path) -> Path:
    return carpeta / ".descargas.json"


def _anotar_descarga(ruta: Path, data: bytes) -> None:
    try:
        manifiesto = json.loads(_manifiesto_descargas(ruta.parent).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifiesto = {}
    if not isinstance(manifiesto, dict):
        manifiesto = {}
    manifiesto[ruta.name] = hashlib.sha256(data).hexdigest()
    try:
        _manifiesto_descargas(ruta.parent).write_text(json.dumps(manifiesto), encoding="utf-8")
    except OSError:
        pass


def _descarga_sin_cambios(ruta: Path) -> bool:
    try:
        manifiesto = json.loads(_manifiesto_descargas(ruta.parent).read_text(encoding="utf-8"))
        return (
            isinstance(manifiesto, dict) and manifiesto.get(ruta.name) == hashlib.sha256(ruta.read_bytes()).hexdigest()
        )
    except (OSError, ValueError):
        return False


def _fetch_item(args: dict) -> tuple[str | None, dict]:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."}), {}
    token, user_site_url, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err), {}

    file_path = str(args.get("file_path") or "").strip().strip("/")
    item_id = str(args.get("item_id") or "").strip()
    drive_id = str(args.get("drive_id") or "").strip()
    site_url = str(args.get("site_url") or user_site_url or "").strip().rstrip("/")

    if item_id:
        item_path = f"/drives/{drive_id}/items/{item_id}" if drive_id else f"/me/drive/items/{item_id}"
    elif file_path:
        if site_url:
            parsed = urllib.parse.urlparse(site_url)
            hostname = parsed.netloc
            site_path = parsed.path.rstrip("/") or "/"
            item_path = f"/sites/{hostname}:{site_path}:/drive/root:/{urllib.parse.quote(file_path)}"
        else:
            item_path = f"/me/drive/root:/{urllib.parse.quote(file_path)}"
    else:
        return json.dumps({"success": False, "error": "Se requiere file_path o item_id."}), {}

    meta = _graph("GET", item_path, token)
    if "error" in meta:
        return _failure_with_candidates(meta["error"], file_path, token), {}
    if "folder" in meta:
        return json.dumps({"success": False, "error": "La ruta es una carpeta; usa sharepoint_list_files."}), {}

    name = str(meta.get("name") or file_path or item_id)
    size = int(meta.get("size") or 0)
    if size > _MAX_DOWNLOAD_BYTES:
        message = (
            f"El archivo pesa {size / (1024 * 1024):.1f} MB; el límite es {_MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB."
        )
        return json.dumps({"success": False, "error": message}), {}

    download_url = meta.get("@microsoft.graph.downloadUrl")
    if download_url and token != BROKER_TOKEN:
        data, derr = _download_bytes(download_url)
    else:
        contenido = f"{item_path}:/content" if ":/" in item_path else f"{item_path}/content"
        data, derr = _download_bytes(f"{GRAPH_BASE}{contenido.replace(' ', '%20')}", token)
    if data is None:
        return json.dumps({"success": False, "error": derr}), {}
    return None, {"name": name, "size": size, "url": meta.get("webUrl"), "data": data}


def _local_file_result(info: dict) -> dict:
    local_path = _save_to_workspace(info["name"], info["data"])
    ext = os.path.splitext(info["name"].lower())[1]
    result = {
        "success": True,
        "name": info["name"],
        "size": info["size"],
        "url": info["url"],
        "local_path": local_path,
        "mime_type": mimetypes.guess_type(info["name"])[0] or "application/octet-stream",
    }
    if ext in _IMAGE_EXTENSIONS:
        result["hint"] = "Imagen guardada en el workspace. Analízala con vision_analyze usando local_path, de a una."
    return result


_SHARE_FOLDER = "UltraBot-archivos"
_MAX_SHARE_BYTES = 50_000_000
_SIMPLE_UPLOAD_MAX_BYTES = 3_900_000
_UPLOAD_CHUNK_BYTES = 327_680 * 10
_SHARE_SCOPES = ("organization", "anonymous")


_HOSTS_SUBIDA = (".sharepoint.com", ".files.1drv.com", "graph.microsoft.com", "api.onedrive.com")


def _url_de_subida_permitida(url: str) -> bool:
    destino = urllib.parse.urlsplit(url)
    host = (destino.hostname or "").lower()
    if destino.scheme == "https" and any(host == h.lstrip(".") or host.endswith(h) for h in _HOSTS_SUBIDA):
        return True
    return os.environ.get("HERMESHQ_ENTORNO") == "aislado" and host in ("127.0.0.1", "localhost")


def _put_binary(url: str, data: bytes, token: str | None = None, content_range: str | None = None) -> dict:
    if token == BROKER_TOKEN:
        if not url.startswith(GRAPH_BASE) or content_range:
            return {"error": "Subida por sesión: usa la URL de carga entregada por el broker."}
        res = _broker_request("PUT", _ruta_graph(url), cuerpo_bytes=data)
        return res if "error" in res else (res.get("json") or {})
    if token is None and content_range and not _url_de_subida_permitida(url):
        return {"error": "La dirección de subida no es de Microsoft; no envío el archivo."}
    headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(data))}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_range:
        headers["Content-Range"] = content_range
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
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
        if exc.code == 403:
            return {
                "error": "Sin permiso para escribir en OneDrive (403): la cuenta necesita el permiso Files.ReadWrite."
            }
        return {"error": f"Error {exc.code} subiendo el archivo: {detail}"}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"error": f"No se pudo subir el archivo: {exc}"}


def _resolve_local_file(raw_path: str) -> tuple[Path | None, str | None]:
    base = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()).resolve()
    candidate = Path(raw_path)
    path = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if base != path and base not in path.parents:
        return None, f"El archivo '{raw_path}' está fuera del directorio de trabajo."
    if not path.is_file():
        return None, f"No existe el archivo '{raw_path}'."
    size = path.stat().st_size
    if size > _MAX_SHARE_BYTES:
        return None, f"El archivo pesa {size / 1_000_000:.1f} MB; el máximo es {_MAX_SHARE_BYTES // 1_000_000} MB."
    return path, None


_PROHIBIDOS_DRIVE = re.compile(r'[\x00-\x1f\x7f"*:<>?/\\|#%]')


def _nombre_en_el_mensaje(guardado: str) -> str | None:
    try:
        payload = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    textos = (
        payload.get("mensaje"),
        metadata.get("mensaje_usuario"),
        payload.get("prompt"),
        payload.get("pedido_anterior"),
    )
    patron = re.compile(r"-\s*([^\n]+?)\s*\([^()\n]*\)\s*->\s*uploads/" + re.escape(guardado) + r"\b")
    for texto in textos:
        coincidencia = patron.search(str(texto or ""))
        if coincidencia and coincidencia.group(1).strip():
            return Path(coincidencia.group(1).strip()).name
    return None


def _nombre_original(path: Path) -> str:
    if path.parent.name != "uploads":
        return path.name
    try:
        nombres = json.loads((path.parent / ".nombres.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        nombres = {}
    original = nombres.get(path.name) if isinstance(nombres, dict) else None
    if original and str(original).strip():
        return str(original).strip()
    return _nombre_en_el_mensaje(path.name) or path.name


_EXTENSIONES_POR_FORMATO = {
    "excel": (".xlsx", ".xlsm", ".csv"),
    "html": (".html", ".htm"),
    "word": (".docx",),
    "pdf": (".pdf",),
}


def _formato_no_pedido(path: Path) -> str | None:
    try:
        formato = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}").get("formato_pedido") or ""
    except (ValueError, AttributeError):
        return None
    permitidas = _EXTENSIONES_POR_FORMATO.get(formato)
    if not permitidas or path.suffix.lower() in permitidas or path.parent.name not in ("outputs", "output"):
        return None
    return (
        f"El usuario pidió el resultado en {formato.upper()}; no subas '{path.name}'. Sube solo el archivo "
        f"{' / '.join(permitidas)}."
    )


def _payload_de_la_tarea() -> dict:
    try:
        payload = json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _plano(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()


_HUELLA_DE_PLANTILLA = re.compile(
    r'<script type="application/json" id="hq-spec">.*?</script>'
    r'<script type="application/json" id="hq-huella">\{"sha256": "([0-9a-f]{64})"\}</script>',
    re.S,
)


_SKILL_NOMBRADA = re.compile(r"\bskill\s+[\"'`]?([a-z0-9][a-z0-9_-]{2,})")


def _nombre_con_extension(display: str, path: Path) -> str:
    extension = path.suffix
    if extension and Path(display).suffix.lower() != extension.lower():
        return f"{display}{extension}"
    return display


def _safe_drive_name(display: str) -> str:
    nombre = _PROHIBIDOS_DRIVE.sub("_", unicodedata.normalize("NFC", str(display or ""))).strip(" .")
    if nombre.startswith("~$"):
        nombre = "_" + nombre[2:]
    return nombre[:120].rstrip(" .") or "archivo"


def _upload_local_file(
    token: str, path: Path, remote_dir: str, safe_name: str, base: str = "/me/drive/root"
) -> tuple[dict | None, str | None]:
    size = path.stat().st_size
    data = path.read_bytes()
    remote = f"{base}:/{remote_dir}/{safe_name}" if remote_dir else f"{base}:/{safe_name}"
    if size <= _SIMPLE_UPLOAD_MAX_BYTES:
        item = _put_binary(
            _encode_url(f"{GRAPH_BASE}{remote}:/content?@microsoft.graph.conflictBehavior=rename"), data, token
        )
    else:
        session = _graph(
            "POST", f"{remote}:/createUploadSession", token, {"item": {"@microsoft.graph.conflictBehavior": "rename"}}
        )
        if "error" in session:
            return None, session["error"]
        upload_url = session.get("uploadUrl")
        if not upload_url:
            return None, "OneDrive no entregó la dirección de subida."
        item = {}
        for start in range(0, size, _UPLOAD_CHUNK_BYTES):
            chunk = data[start : start + _UPLOAD_CHUNK_BYTES]
            item = _put_binary(upload_url, chunk, None, f"bytes {start}-{start + len(chunk) - 1}/{size}")
            if "error" in item:
                break
    if "error" in item:
        return None, item["error"]
    return item, None


_WINDOWS_ONEDRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]+Users[\\/]+[^\\/]+[\\/]+OneDrive[^\\/]*[\\/]*(.*)$", re.I)


def _normalize_drive_folder(raw: str) -> tuple[str | None, str | None]:
    """Convierte una ruta de Windows o de OneDrive en una ruta relativa segura a la raíz del OneDrive."""
    text = str(raw or "").strip()
    match = _WINDOWS_ONEDRIVE_RE.match(text)
    if match:
        text = match.group(1)
    parts = [p.strip() for p in re.split(r"[\\/]+", text) if p.strip()]
    if len(parts) > 10:
        return None, "La carpeta tiene demasiados niveles (máximo 10)."
    for part in parts:
        if part in (".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', part):
            return None, f"Nombre de carpeta no válido: '{part}'."
    return "/".join(parts), None


_PIDE_CARPETA_NUEVA = re.compile(
    r"\b(crea|creas|crear|creame|cree|creen|haz|hazme|arma|armame)\b[^.\n]{0,40}\bcarpetas?\b|"
    r"\bcarpetas? nuevas?\b|\bnuevas? carpetas?\b|\bcrea(la|lo|las|los)\b|\bcrear (la|esa|esta)\b"
)


def _raiz_de_la_carpeta(token: str, folder: str) -> str | None:
    primera = (folder or "").split("/", 1)[0].strip()
    if not primera:
        return None
    try:
        datos = _graph("GET", _encode_url(f"/me/drive/root:/{primera}") + "?$select=id,name,folder,remoteItem", token)
    except Exception:
        return None
    if not isinstance(datos, dict):
        return None
    if "error" in datos:
        return "no_existe" if "itemNotFound" in str(datos.get("error")) else None
    return "atajo" if datos.get("remoteItem") else None


_RUTA_ONEDRIVE_DEL_USUARIO = re.compile(r"[a-z]:\\+users\\+[^\\]+\\+onedrive[^\\]*\\+([^\\\n]+)")
_RUTA_SUELTA_DEL_USUARIO = re.compile(r"([^\\\n]+?)\\+[^\\\n]")


def _textos_del_pedido() -> list[str]:
    payload = _payload_de_la_tarea()
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    textos = (
        metadata.get("mensaje_usuario"),
        payload.get("prompt"),
        payload.get("mensaje"),
        payload.get("pedido_anterior"),
    )
    return [_plano(str(t)) for t in textos if str(t or "").strip()]


_NO_SUBIR = re.compile(
    r"\bno\s+(?:me\s+)?(?:lo|la|los|las)?\s*(?:subas|suban|subir|guardes|guarden|cargues|carguen)\b"
    r"(?:\s+(?:todavia|aun|por\s+ahora))?\s*"
    r"(?:(?:a|en|al)\s+(?:mi\s+|tu\s+|su\s+|el\s+|la\s+)?(?:onedrive|sharepoint|nube)\b|[.,;:!)\n]|$)"
)
_SOLO_EN_EL_CHAT = re.compile(
    r"\bsolo\s+(?:adjuntal[oa]s?|adjunta(?:lo|la)?|en\s+el\s+chat|por\s+(?:aqui|el\s+chat))\b"
)


_PIDE_SUBIR_AHORA = re.compile(
    r"\b(?:sub(?:e|elo|ela|elos|elas|ir|irlo|irla)|guard(?:a|alo|ala|alos|alas)\s+en\s+(?:mi\s+|el\s+|la\s+)?"
    r"(?:onedrive|sharepoint|carpeta))\b"
)
_NO_SUBIR_TTL = 8 * 3600


_ADJUNTAR_AQUI = re.compile(
    r"\badjunt(?:a|alo|ala|alos|alas|ar|arlo|arla|arlos|arlas|e|elo|ela)\s+(?:aqui|en\s+el\s+chat|por\s+aqui)\b"
)


def _prohibicion_en(texto) -> str | None:
    plano = _plano(str(texto or ""))
    coincidencia = _NO_SUBIR.search(plano) or _SOLO_EN_EL_CHAT.search(plano)
    if (
        not coincidencia
        and not _PIDE_SUBIR_AHORA.search(plano)
        and not re.search(r"onedrive|sharepoint|\bguard\w*|\bcarpeta\b|\bsub\w+", plano)
    ):
        coincidencia = _ADJUNTAR_AQUI.search(plano)
    return coincidencia.group(0).strip(" .,;:!)\n") if coincidencia else None


def _conversacion_de_la_tarea(metadata: dict) -> str:
    sesion = str(metadata.get("public_session_id") or metadata.get("origen_public_session_id") or "")
    return f"{sesion}#{metadata.get('conversation_epoch') or 0}" if sesion else ""


def _marcas_no_subir_path() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".no_subir.json"


def _marcas_no_subir() -> dict:
    try:
        datos = json.loads(_marcas_no_subir_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ahora = time.time()
    if not isinstance(datos, dict):
        return {}
    return {k: v for k, v in datos.items() if isinstance(v, dict) and ahora - float(v.get("ts") or 0) < _NO_SUBIR_TTL}


def _guardar_marcas_no_subir(marcas: dict) -> None:
    ruta = _marcas_no_subir_path()
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        temporal = ruta.with_suffix(".tmp")
        temporal.write_text(json.dumps(marcas, ensure_ascii=False), encoding="utf-8")
        os.replace(temporal, ruta)
    except OSError:
        pass


def _prohibio_subir() -> str | None:
    payload = _payload_de_la_tarea()
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    conversacion = _conversacion_de_la_tarea(metadata)
    marcas = _marcas_no_subir()
    for texto in (metadata.get("mensaje_usuario"), metadata.get("pedido_del_usuario")):
        frase = _prohibicion_en(texto)
        if frase:
            if conversacion:
                marcas[conversacion] = {"frase": frase, "ts": time.time()}
                _guardar_marcas_no_subir(marcas)
            return frase
    actual = _plano(str(metadata.get("mensaje_usuario") or metadata.get("pedido_del_usuario") or ""))
    if _PIDE_SUBIR_AHORA.search(actual):
        if conversacion in marcas:
            marcas.pop(conversacion)
            _guardar_marcas_no_subir(marcas)
        return None
    if conversacion in marcas:
        return str(marcas[conversacion].get("frase") or "no lo subas")
    return _prohibicion_en(payload.get("pedido_anterior"))


def _carpeta_inventada(folder: str) -> bool:
    textos = _textos_del_pedido()
    if not textos:
        return False
    if any(_PIDE_CARPETA_NUEVA.search(t) for t in textos):
        return False
    primera = _plano((folder or "").split("/", 1)[0]).strip(" .")
    if not primera:
        return False
    inicios = []
    for texto in textos:
        for linea in texto.splitlines():
            exacta = _RUTA_ONEDRIVE_DEL_USUARIO.search(linea)
            if exacta:
                inicios.append(("exacta", exacta.group(1).strip(" .\"'")))
                continue
            suelta = _RUTA_SUELTA_DEL_USUARIO.search(linea)
            if suelta and not re.search(r"[a-z]:\\", linea):
                inicios.append(("suelta", suelta.group(1).strip(" .\"'")))
    if inicios:
        return not any(
            inicio == primera or (tipo == "suelta" and inicio.endswith(" " + primera)) for tipo, inicio in inicios
        )
    return not any(primera in t for t in textos)


def _error_biblioteca(primera: str) -> str:
    return (
        f"«{primera}» es una biblioteca de un sitio de SharePoint sincronizada o un acceso directo, no una carpeta "
        "de tu OneDrive: todavía no puedo subir ahí. No lo subas a otra carpeta: dile al usuario que no pude "
        "guardarlo en esa ruta y ofrécele guardarlo en su OneDrive."
    )


_HOST_SHAREPOINT = re.compile(r"^[a-z0-9-]+\.sharepoint\.com$", re.I)
_SITIO_NO_IDENTIFICADO = (
    "No pude identificar el sitio de SharePoint de esa ruta. Pásame la dirección del sitio (site_url, p. ej. "
    "https://empresa.sharepoint.com/sites/TI) o dile al usuario que no pude guardarlo ahí."
)
_BIBLIOTECAS_POR_DEFECTO = ("documentos", "documents", "documentoscompartidos", "shareddocuments")


def _sitios_candidatos(titulo: str, token: str) -> list[str]:
    mio = _graph("GET", "/me/drive?$select=webUrl", token)
    web = str(mio.get("webUrl") or "") if isinstance(mio, dict) else ""
    host = urllib.parse.urlparse(web).netloc.replace("-my.sharepoint.com", ".sharepoint.com")
    if not _HOST_SHAREPOINT.match(host or ""):
        return []
    base = re.sub(r"\s+", " ", titulo).strip()
    nombres = []
    for nombre in (
        base.replace(" ", ""),
        base.replace(" ", "-"),
        base.replace(" ", "").lower(),
        base.replace(" ", "_"),
    ):
        if nombre and nombre not in nombres:
            nombres.append(nombre)
    return [f"https://{host}/sites/{n}" for n in nombres]


def _abrir_sitio(site_url: str, token: str):
    analizada = urllib.parse.urlparse(site_url)
    if analizada.scheme != "https" or not _HOST_SHAREPOINT.match(analizada.netloc or ""):
        return None
    sitio = _graph(
        "GET", f"/sites/{analizada.netloc}:{analizada.path.rstrip('/') or '/'}?$select=id,displayName", token
    )
    if not isinstance(sitio, dict) or "error" in sitio or not sitio.get("id"):
        return None
    return sitio


def _biblioteca_de_destino(site_url: str, folder: str, token: str) -> tuple[str | None, str, str, str, str | None]:
    partes = [p for p in folder.split("/") if p]
    if not partes:
        return (
            None,
            "",
            "",
            "",
            "Indica la carpeta de destino empezando por el nombre de la biblioteca o la carpeta del sitio.",
        )
    primera = partes[0]
    titulo, _, sufijo = primera.partition(" - ")
    candidatos = [site_url] if site_url else _sitios_candidatos(titulo, token) if sufijo else []
    sitio, direccion = None, ""
    for candidato in candidatos:
        abierto = _abrir_sitio(candidato, token)
        if abierto and (site_url or _fold_name(abierto.get("displayName")) == _fold_name(titulo)):
            sitio, direccion = abierto, candidato
            break
    if not sitio:
        return None, "", "", "", _SITIO_NO_IDENTIFICADO
    unidades = _graph("GET", f"/sites/{sitio['id']}/drives?$select=id,name", token).get("value", [])
    nombres = {_fold_name(primera), _fold_name(sufijo)} - {""}
    for unidad in unidades:
        if _fold_name(unidad.get("name")) in nombres:
            return unidad["id"], "/".join(partes[1:]), str(unidad.get("name") or ""), direccion, None
    por_defecto = next(
        (u for u in unidades if _fold_name(u.get("name")) in _BIBLIOTECAS_POR_DEFECTO),
        unidades[0] if unidades else None,
    )
    if por_defecto is None:
        return None, "", "", direccion, "El sitio no tiene bibliotecas de documentos accesibles."
    carpeta = "/".join(([sufijo] if sufijo else [primera]) + partes[1:])
    primera_carpeta = carpeta.split("/", 1)[0]
    existe = _graph(
        "GET", _encode_url(f"/drives/{por_defecto['id']}/root:/{primera_carpeta}") + "?$select=id,folder", token
    )
    if not isinstance(existe, dict) or "error" in existe:
        nombres_bib = ", ".join(str(u.get("name")) for u in unidades)
        return (
            None,
            "",
            "",
            direccion,
            (
                f"En el sitio no encontré la carpeta «{primera_carpeta}» en la biblioteca «{por_defecto.get('name')}» ni una "
                f"biblioteca con ese nombre (bibliotecas: {nombres_bib}). No creo un árbol nuevo: revisa la ruta con "
                "sharepoint_list_files."
            ),
        )
    return por_defecto["id"], carpeta, str(por_defecto.get("name") or ""), direccion, None


_PARAMETROS_SUBIDA = frozenset({"file_path", "folder", "name", "site_url"})
_ID_DE_GRAPH = re.compile(r"^(01[A-Z0-9]{30,}|b![\w-]{20,})$")


def _destino_por_id(args: dict) -> str | None:
    for clave, valor in (args or {}).items():
        texto = str(valor or "").strip()
        if not texto:
            continue
        if clave == "folder":
            if _ID_DE_GRAPH.match(texto):
                return clave
            continue
        if clave in _PARAMETROS_SUBIDA:
            continue
        if clave.lower().endswith("id") or _ID_DE_GRAPH.match(texto):
            return clave
    return None


def _subir_a_biblioteca_de_sitio(
    token: str, path: Path, display: str, destino: tuple[str | None, str, str, str, str | None]
) -> str:
    drive_id, carpeta, biblioteca, direccion, error = destino
    if error:
        return json.dumps({"success": False, "error": error}, ensure_ascii=False)
    item, upload_error = _upload_local_file(token, path, carpeta, _safe_drive_name(display), f"/drives/{drive_id}/root")
    if upload_error:
        texto = str(upload_error)
        if "subida_fuera_del_onedrive_propio" in texto:
            texto = (
                "Este agente aún no tiene habilitado guardar en bibliotecas de sitios de SharePoint (el administrador lo "
                "activa con sharepoint.subida_a_sitios). Dile al usuario que no pude guardarlo ahí y ofrécele su OneDrive."
            )
        elif "accion_ya_ejecutada" in texto:
            texto = "Ese mismo archivo ya se subió a esa carpeta hace un momento; no lo dupliqué."
        return json.dumps({"success": False, "error": texto}, ensure_ascii=False)
    return json.dumps(
        {
            "success": True,
            "name": item.get("name"),
            "sitio": direccion,
            "biblioteca": biblioteca,
            "folder": carpeta or "(raíz de la biblioteca)",
            "size": path.stat().st_size,
            "url": item.get("webUrl"),
            "note": "Guardado en el sitio de SharePoint. Si ya existía un archivo con ese nombre, se guardó con otro.",
        },
        ensure_ascii=False,
    )


_REVISOR_HTML = (os.environ.get("HERMESHQ_REVISOR_HTML", ""), "/app/hermeshq/assets/revisor-html/revisar.js")


def _revisar_html_js(path: Path) -> dict | None:
    script = next((c for c in _REVISOR_HTML if c and Path(c).is_file()), None)
    nodo = shutil.which("node")
    if not script or not nodo:
        return None
    temporal = tempfile.gettempdir()
    try:
        proceso = subprocess.run(
            [nodo, "--max-old-space-size=512", script, str(path.resolve())],
            capture_output=True,
            text=True,
            timeout=40,
            cwd=temporal,
            env={"PATH": os.environ.get("PATH", ""), "HOME": temporal},
        )
        resultado = json.loads(proceso.stdout or "null")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if isinstance(resultado, dict) and isinstance(resultado.get("errores_carga"), list):
        return resultado
    return None


def _html_con_errores_js(path: Path) -> str | None:
    if path.suffix.lower() not in (".html", ".htm") or _DOWNLOAD_DIR in path.parts:
        return None
    revision = _revisar_html_js(path)
    if not revision or not revision["errores_carga"]:
        return None
    error = revision["errores_carga"][0]
    detalle = f"línea {error['linea']}: {error.get('mensaje', '')}" if error.get("linea") else str(error.get("mensaje"))
    if error.get("codigo"):
        detalle += f" — «{error['codigo']}»"
    return (
        f"No subí '{path.name}': al abrirlo en un navegador de prueba su JavaScript falla ({detalle}), así "
        "que el usuario vería la página vacía, incompleta o sin responder. Corrige ese archivo: lee esa línea "
        "con read_file y arréglala con patch (un SyntaxError suele estar en esa línea o justo antes: una comilla "
        "invertida ` o un paréntesis sin cerrar) y vuelve a subirlo. No digas que quedó subido."
    )


def _upload_file_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, sitio_asignado, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    raw_path = str(args.get("file_path") or "").strip()
    if not raw_path:
        return json.dumps({"success": False, "error": "Se requiere file_path (archivo del directorio de trabajo)."})
    candidato = _ruta_en_espacio_de_subida(raw_path)
    if candidato is not None and candidato.parent.name == _DOWNLOAD_DIR and _descarga_sin_cambios(candidato):
        return json.dumps(
            {
                "success": False,
                "error": "Ese archivo es el que descargaste de OneDrive/SharePoint, sin ningún cambio: subirlo solo "
                "crearía una copia idéntica. Si había que modificarlo y la modificación falló, dile al usuario que no "
                "se pudo hacer el cambio.",
            },
            ensure_ascii=False,
        )
    prohibicion = _prohibio_subir()
    if prohibicion:
        return json.dumps(
            {
                "success": False,
                "error": f"El usuario pidió no subirlo («{prohibicion}»). No lo subas: adjúntalo en el chat con su "
                "línea MEDIA:<ruta> y di que quedó adjunto, no en OneDrive. Si ahora quiere subirlo, tiene que "
                "pedirlo él con esas palabras.",
            },
            ensure_ascii=False,
        )
    if _destino_por_id(args):
        return json.dumps(
            {
                "success": False,
                "error": "sharepoint_upload_file sube solo al OneDrive del usuario, por la ruta de carpeta (folder); no "
                "acepta item_id ni drive_id. Para una biblioteca de un sitio de SharePoint todavía no puedo subir: dile al "
                "usuario que no puedo guardarlo ahí y ofrécele su OneDrive.",
            },
            ensure_ascii=False,
        )
    carpeta_pedida = args.get("folder") if args.get("folder") not in (None, "") else args.get("folder_path")
    folder, folder_error = _normalize_drive_folder(carpeta_pedida)
    if folder_error:
        return json.dumps({"success": False, "error": folder_error}, ensure_ascii=False)
    path, path_error = _resolve_local_file(raw_path)
    if path_error:
        return json.dumps({"success": False, "error": path_error}, ensure_ascii=False)
    formato_error = _formato_no_pedido(path) or _html_con_errores_js(path)
    if formato_error:
        return json.dumps({"success": False, "error": formato_error}, ensure_ascii=False)
    display = _nombre_con_extension(
        str(args.get("name") or _nombre_original(path)).strip() or _nombre_original(path), path
    )
    site_url = str(args.get("site_url") or "").strip().rstrip("/")
    raiz = _raiz_de_la_carpeta(token, folder) if folder and not site_url else None
    primera = (folder or "").split("/", 1)[0]
    if raiz == "atajo" and not site_url and sitio_asignado:
        site_url = sitio_asignado
    if site_url or (" - " in primera and raiz in ("atajo", "no_existe")):
        destino = _biblioteca_de_destino(site_url, folder, token)
        if not (destino[4] == _SITIO_NO_IDENTIFICADO and not site_url):
            return _subir_a_biblioteca_de_sitio(token, path, display, destino)
    if raiz == "atajo":
        return json.dumps({"success": False, "error": _error_biblioteca(primera)}, ensure_ascii=False)
    if raiz == "no_existe" and _carpeta_inventada(folder):
        return json.dumps(
            {
                "success": False,
                "error": f"La carpeta «{primera}» no existe en la raíz del OneDrive del usuario: subir ahí crearía un "
                f"árbol nuevo «{folder}» que él no va a encontrar. No la crees por tu cuenta. Revisa la ruta (las "
                "rutas de Windows suelen empezar en Escritorio o Documentos) con sharepoint_list_files, o pregúntale "
                "si quiere crear esa carpeta nueva.",
            },
            ensure_ascii=False,
        )
    item, upload_error = _upload_local_file(token, path, folder, _safe_drive_name(display))
    ya_estaba = False
    if upload_error and token == BROKER_TOKEN and "accion_ya_ejecutada" in str(upload_error):
        remoto = (
            f"/me/drive/root:/{folder}/{_safe_drive_name(display)}"
            if folder
            else f"/me/drive/root:/{_safe_drive_name(display)}"
        )
        existente = _graph("GET", _encode_url(remoto) + "?$select=id,name,webUrl", token)
        if "error" not in existente and existente.get("id"):
            item, upload_error, ya_estaba = existente, None, True
    if upload_error:
        if "itemNotFound" in str(upload_error) and " - " in primera:
            upload_error = _error_biblioteca(primera)
        return json.dumps({"success": False, "error": upload_error}, ensure_ascii=False)
    if ya_estaba:
        return json.dumps(
            {
                "success": True,
                "ya_estaba_subido": True,
                "name": item.get("name"),
                "folder": folder or "(raíz de OneDrive)",
                "size": path.stat().st_size,
                "url": item.get("webUrl"),
                "note": "Ese mismo archivo ya se había subido a esa carpeta hace un momento; no lo dupliqué.",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "success": True,
            "name": item.get("name"),
            "folder": folder or "(raíz de OneDrive)",
            "size": path.stat().st_size,
            "url": item.get("webUrl"),
            "note": "Si OneDrive está sincronizado en su equipo, el archivo aparece solo en esa carpeta. Si ya existía uno con el mismo nombre, se guardó con otro nombre; nada se sobrescribe.",
        },
        ensure_ascii=False,
    )


def _llamar_control(metodo: str, ruta: str, payload: dict | None = None, timeout: int = 90) -> dict:
    base_url, headers, _tarea = _broker_headers()
    if not base_url:
        return {"ok": False, "codigo": "sin_control", "error": "HermesHQ internal control no configurado"}
    datos = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{base_url}{ruta}", data=datos, method=metodo, headers={**headers, "Content-Type": "application/json"}
    )
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
            return {"ok": False, "codigo": codigo, "error": detalle.get("error") or codigo}
        return {"ok": False, "codigo": "error", "error": str(detalle)[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "codigo": "sin_respuesta", "error": f"{type(exc).__name__}: {exc}"}


def _aprobacion_vigente_del_archivo(args: dict, data: bytes, scope: str) -> tuple[str | None, dict]:
    r = _llamar_control(
        "GET", f"/control/aprobaciones/aprobadas?task_id={urllib.parse.quote(_task_id() or '')}&tipo=graph.onedrive"
    )
    prefijo = hashlib.sha256(data).hexdigest()[:8] + "_"
    for aprob in reversed(r.get("aprobadas") or [] if r.get("ok") else []):
        contenido = aprob.get("contenido") or {}
        if (
            str((contenido.get("archivo") or {}).get("nombre") or "").startswith(prefijo)
            and (contenido.get("cuerpo") or {}).get("scope") == scope
        ):
            m = re.match(r"^/me/drive/items/([^/]+)/createLink$", str(contenido.get("ruta") or ""))
            if m:
                return m.group(1), dict(args, aprobacion_id=aprob["id"])
    return None, args


def _item_aprobado(args: dict, data: bytes) -> str | None:
    r = _llamar_control(
        "GET",
        f"/control/aprobaciones/{urllib.parse.quote(str(args.get('aprobacion_id')))}?task_id="
        f"{urllib.parse.quote(_task_id() or '')}",
    )
    contenido = r.get("contenido") or {} if r.get("ok") else {}
    nombre = str((contenido.get("archivo") or {}).get("nombre") or "")
    if r.get("tipo_accion") != "graph.onedrive" or not nombre.startswith(hashlib.sha256(data).hexdigest()[:8] + "_"):
        return None
    m = re.match(r"^/me/drive/items/([^/]+)/createLink$", str(contenido.get("ruta") or ""))
    return m.group(1) if m else None


def _compartir_con_aprobacion(args: dict, item_id: str, scope: str):
    contenido = {
        "metodo": "POST",
        "ruta": f"/me/drive/items/{item_id}/createLink",
        "cuerpo": {"type": "view", "scope": scope},
    }
    aprobacion_id = str(args.get("aprobacion_id") or "").strip()
    if aprobacion_id:
        r = _llamar_control(
            "POST",
            "/control/m365/accion/ejecutar",
            {"tarea_id": _task_id(), "aprobacion_id": aprobacion_id, "contenido": contenido},
        )
        if r.get("ok"):
            resultado = r.get("resultado") or {}
            if r.get("ya_aplicada"):
                return {"link": {"webUrl": resultado.get("id")}}
            return resultado
        codigo = r.get("codigo")
        if codigo in ("ya_ejecutada", "accion_ya_ejecutada"):
            return json.dumps(
                {
                    "success": False,
                    "ya_ejecutada": True,
                    "error": "Ese enlace ya se creó con esta aprobación: no lo repetí.",
                },
                ensure_ascii=False,
            )
        if codigo in ("resultado_incierto", "sin_respuesta", "conciliacion_manual_requerida", "conciliacion_prematura"):
            return json.dumps(
                {
                    "success": False,
                    "estado": "desconocido",
                    "codigo": codigo,
                    "error": "No sé si el enlace se creó. No lo repito: primero se verifica.",
                },
                ensure_ascii=False,
            )
        if codigo in ("rechazada", "pendiente", "operacion_bloqueada"):
            return json.dumps(
                {"success": False, "codigo": codigo, "error": r.get("error") or codigo}, ensure_ascii=False
            )
        if codigo == "fallida":
            return json.dumps(
                {
                    "success": False,
                    "codigo": codigo,
                    "error": f"El enlace NO se creó. {r.get('error') or ''} No se repitió. "
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
            "task_id": _task_id(),
            "tipo": "graph.onedrive",
            "contenido": contenido,
            "parametros": {
                "herramienta": "sharepoint_share_file",
                "args": {k: v for k, v in args.items() if k != "aprobacion_id"},
            },
        },
    )
    if not creada.get("ok") or not creada.get("id"):
        return json.dumps(
            {"success": False, "codigo": creada.get("codigo"), "error": creada.get("error")}, ensure_ascii=False
        )
    if creada.get("estado") == "ejecutada" and not args.get("_reintento_aprobada"):
        return _compartir_con_aprobacion(
            dict(args, aprobacion_id=creada["id"], _reintento_aprobada=True), item_id, scope
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
        return _compartir_con_aprobacion(
            dict(args, aprobacion_id=creada["id"], _reintento_aprobada=True), item_id, scope
        )
    salida = {
        "success": False,
        "requiere_aprobacion": True,
        "aprobacion_id": creada["id"],
        "borrador": creada.get("vista_previa"),
        "siguiente": "Muestra el campo 'borrador' TAL CUAL y pregunta si comparte el archivo. Solo el usuario "
        "aprueba. Después vuelve a llamar con aprobacion_id y exactamente el mismo archivo y alcance.",
    }
    if aprobacion_id:
        salida["error"] = "El archivo o el alcance cambiaron o la aprobación ya no vale: se pidió una aprobación nueva."
    if creada.get("tarjeta"):
        salida["tarjeta_aprobacion"] = creada["tarjeta"]
    return json.dumps(salida, ensure_ascii=False)


def _archivo_local_por_nombre_remoto(base: Path, raw_path: str) -> Path | None:
    m = re.match(r"^(?:.*/)?([0-9a-f]{8})_(.+)$", raw_path.replace("\\", "/"))
    if not m:
        return None
    prefijo, nombre = m.group(1), m.group(2)
    for candidato in [*base.glob(nombre), *base.glob(f"*/{nombre}")]:
        try:
            if candidato.is_file() and hashlib.sha256(candidato.read_bytes()).hexdigest().startswith(prefijo):
                return candidato.resolve()
        except OSError:
            continue
    return None


_CONFIRMACION_TTL_SEGUNDOS = 1800


def _aprobacion_central() -> bool:
    return os.environ.get("HERMESHQ_APROBACION_CENTRAL", "").strip() == "1"


def _tarea_automatica() -> bool:
    try:
        meta = (json.loads(os.environ.get("HERMESHQ_TASK_PAYLOAD") or "{}") or {}).get("metadata") or {}
    except (ValueError, AttributeError):
        return False
    return bool(meta.get("scheduled") or meta.get("background_job"))


def _ruta_confirmaciones() -> Path:
    return Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()) / "work" / ".sharepoint_share_previews.json"


def _confirmaciones() -> dict:
    try:
        datos = json.loads(_ruta_confirmaciones().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    limite = time.time() - _CONFIRMACION_TTL_SEGUNDOS
    return {k: v for k, v in datos.items() if isinstance(v, dict) and float(v.get("ts") or 0) >= limite}


def _guardar_confirmaciones(datos: dict) -> None:
    try:
        ruta = _ruta_confirmaciones()
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(datos), encoding="utf-8")
    except OSError:
        pass


def _registrar_vista_previa_en_servidor(huella: str, alcance: str) -> str | None:
    base_url, headers, tarea = _broker_headers()
    if not base_url or not tarea:
        return "No hay tarea activa para registrar la vista previa."
    req = urllib.request.Request(
        f"{base_url}/control/m365/compartir/vista-previa",
        data=json.dumps({"tarea_id": tarea, "huella": huella, "alcance": alcance}).encode("utf-8"),
        method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            return None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detalle = json.loads(raw).get("detail", raw)
        except (json.JSONDecodeError, ValueError, AttributeError):
            detalle = raw
        motivo = detalle.get("motivo") if isinstance(detalle, dict) else str(detalle)
        return f"No se pudo preparar la confirmación de compartir ({motivo})."
    except Exception as exc:  # noqa: BLE001  # HTTP request catch-all
        return f"No se pudo preparar la confirmación de compartir: {exc}"


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
    "comparte",
    "compartelo",
    "compartela",
    "compartir",
    "compartirlo",
    "genera",
    "generalo",
    "crea",
    "crealo",
    "envia",
    "envialo",
    "manda",
    "mandalo",
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
    "archivo",
    "enlace",
    "link",
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


def _puerta_de_compartir(args: dict, huella: str, vista: dict, alcance: str | None = None) -> str | None:
    if _tarea_automatica():
        return json.dumps(
            {
                "success": False,
                "error": "No comparto archivos en una tarea programada o en segundo plano: no hay un "
                "usuario que confirme el enlace.",
            },
            ensure_ascii=False,
        )
    pendientes = _confirmaciones()
    previa = pendientes.get(huella)
    actual = _task_id()
    previa_de_otra_tarea = bool(previa and actual and previa.get("task") not in (None, actual))
    if args.get("aprobado_por_el_usuario") and previa_de_otra_tarea and _usuario_confirma():
        return None
    if alcance:
        fallo = _registrar_vista_previa_en_servidor(huella, alcance)
        if fallo:
            return json.dumps({"success": False, "error": fallo}, ensure_ascii=False)
    pendientes[huella] = {"task": actual, "ts": time.time()}
    _guardar_confirmaciones(pendientes)
    salida = {
        "success": False,
        "requiere_aprobacion": True,
        "vista_previa": vista,
        "siguiente": "Muéstrale al usuario esta vista previa y pregunta si comparte el archivo; termina tu respuesta "
        "ahí. Cuando diga que sí, vuelve a llamar con aprobado_por_el_usuario=true y EXACTAMENTE los mismos datos.",
    }
    if args.get("aprobado_por_el_usuario"):
        salida["error"] = (
            "No se compartió: el último mensaje del usuario no es una confirmación clara (no dijo «sí», o lo negó, "
            "canceló o pidió esperar). No lo compartas ni digas que se compartió: pregúntale si lo comparte."
            if previa_de_otra_tarea
            else "No se compartió: el usuario aún no vio esta vista previa en un mensaje anterior. No puedes confirmar "
            "por él."
        )
    return json.dumps(salida, ensure_ascii=False)


def _consumir_confirmacion(huella: str) -> None:
    pendientes = _confirmaciones()
    pendientes.pop(huella, None)
    _guardar_confirmaciones(pendientes)


def _share_file_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, _site, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)
    raw_path = str(args.get("file_path") or "").strip()
    if not raw_path:
        return json.dumps({"success": False, "error": "Se requiere file_path (archivo del directorio de trabajo)."})
    scope = str(args.get("scope") or "organization").strip().lower()
    if scope not in _SHARE_SCOPES:
        return json.dumps({"success": False, "error": "scope debe ser organization o anonymous."})
    base = Path(os.environ.get("HERMES_SESSION_CWD") or os.getcwd()).resolve()
    candidate = Path(raw_path)
    path = (candidate if candidate.is_absolute() else base / candidate).resolve()
    if base != path and base not in path.parents:
        return json.dumps({"success": False, "error": f"El archivo '{raw_path}' está fuera del directorio de trabajo."})
    if not path.is_file():
        path = _archivo_local_por_nombre_remoto(base, raw_path) or path
    if not path.is_file():
        return json.dumps({"success": False, "error": f"No existe el archivo '{raw_path}'."})
    size = path.stat().st_size
    if size > _MAX_SHARE_BYTES:
        return json.dumps(
            {
                "success": False,
                "error": f"El archivo pesa {size / 1_000_000:.1f} MB; el máximo es {_MAX_SHARE_BYTES // 1_000_000} MB.",
            }
        )
    display = _nombre_con_extension(
        str(args.get("name") or _nombre_original(path)).strip() or _nombre_original(path), path
    )
    safe = _safe_drive_name(display)
    data = path.read_bytes()
    huella_confirmacion = None
    if token == BROKER_TOKEN and not _aprobacion_central():
        huella_confirmacion = hashlib.sha256(
            json.dumps([user_id, hashlib.sha256(data).hexdigest(), safe, scope]).encode()
        ).hexdigest()
        alcance = "solo personas de la organización" if scope == "organization" else "cualquiera con el enlace"
        vista = {
            "archivo": safe,
            "tamano_bytes": size,
            "carpeta": _SHARE_FOLDER,
            "quien_puede_abrirlo": alcance,
            "importante": (
                "Esto sube una copia a tu OneDrive y crea un enlace; NO se envía a nadie. Si quieres que le llegue a "
                "una persona, el envío por Teams o por correo es un paso aparte que también te muestro antes de enviar."
            ),
        }
        puerta = _puerta_de_compartir(args, huella_confirmacion, vista, scope)
        if puerta is not None:
            return puerta
    aprobado = None
    if token == BROKER_TOKEN and huella_confirmacion is None:
        if args.get("aprobacion_id"):
            aprobado = _item_aprobado(args, data)
        else:
            aprobado, args = _aprobacion_vigente_del_archivo(args, data, scope)
    if aprobado:
        link = _compartir_con_aprobacion(args, aprobado, scope)
        if isinstance(link, str):
            return link
        if "error" in link:
            return json.dumps({"success": False, "error": link["error"]}, ensure_ascii=False)
        url = (link.get("link") or {}).get("webUrl")
        if not url:
            return json.dumps({"success": False, "error": "OneDrive no devolvió el enlace para compartir."})
        result = {"success": True, "name": safe, "size": size, "url": url, "scope": scope}
        if scope == "organization":
            result["note"] = "Enlace solo para personas de la organización; destinatarios externos no podrán abrirlo."
        result["envio"] = (
            "El enlace NO se envió a nadie ni se dio acceso a una persona concreta. Para compartirlo con alguien, "
            "envíaselo por Teams o por correo (con vista previa y aprobación)."
        )
        return json.dumps(result, ensure_ascii=False)
    conflicto = "rename"
    if token == BROKER_TOKEN:
        safe = f"{hashlib.sha256(data).hexdigest()[:8]}_{safe}"
        conflicto = "replace"
    remote = f"/me/drive/root:/{_SHARE_FOLDER}/{safe}"
    if size <= _SIMPLE_UPLOAD_MAX_BYTES:
        item = _put_binary(
            _encode_url(f"{GRAPH_BASE}{remote}:/content?@microsoft.graph.conflictBehavior={conflicto}"), data, token
        )
    else:
        session = _graph(
            "POST", f"{remote}:/createUploadSession", token, {"item": {"@microsoft.graph.conflictBehavior": conflicto}}
        )
        if "error" in session:
            return json.dumps({"success": False, "error": session["error"]}, ensure_ascii=False)
        upload_url = session.get("uploadUrl")
        if not upload_url:
            return json.dumps({"success": False, "error": "OneDrive no entregó la dirección de subida."})
        item = {}
        for start in range(0, size, _UPLOAD_CHUNK_BYTES):
            chunk = data[start : start + _UPLOAD_CHUNK_BYTES]
            item = _put_binary(upload_url, chunk, None, f"bytes {start}-{start + len(chunk) - 1}/{size}")
            if "error" in item:
                break
    if "error" in item and token == BROKER_TOKEN and "accion_ya_ejecutada" in str(item["error"]):
        item = _graph("GET", f"{remote}?$select=id,name", token)
    if "error" in item:
        return json.dumps({"success": False, "error": item["error"]}, ensure_ascii=False)
    item_id = item.get("id")
    if not item_id:
        return json.dumps({"success": False, "error": "OneDrive no devolvió el identificador del archivo."})
    if token == BROKER_TOKEN and huella_confirmacion is None:
        link = _compartir_con_aprobacion(args, item_id, scope)
        if isinstance(link, str):
            return link
    elif huella_confirmacion:
        res = _broker_request(
            "POST",
            f"/me/drive/items/{item_id}/createLink",
            {"type": "view", "scope": scope},
            confirmacion_compartir=huella_confirmacion,
        )
        link = res if "error" in res else (res.get("json") or {})
        if "error" not in link:
            _consumir_confirmacion(huella_confirmacion)
    else:
        link = _graph("POST", f"/me/drive/items/{item_id}/createLink", token, {"type": "view", "scope": scope})
    if "error" in link:
        return json.dumps({"success": False, "error": link["error"]}, ensure_ascii=False)
    url = (link.get("link") or {}).get("webUrl")
    if not url:
        return json.dumps({"success": False, "error": "OneDrive no devolvió el enlace para compartir."})
    result = {"success": True, "name": item.get("name") or safe, "size": size, "url": url, "scope": scope}
    if scope == "organization":
        result["note"] = "Enlace solo para personas de la organización; destinatarios externos no podrán abrirlo."
    return json.dumps(result, ensure_ascii=False)


def _download_file_tool(args: dict, **_kwargs) -> str:
    error, info = _fetch_item(args)
    if error:
        return error
    return json.dumps(_local_file_result(info), ensure_ascii=False)


_FOLDER_DEFAULT_FILES = 40
_FOLDER_HARD_FILES = 100
_FOLDER_DEFAULT_MB = 150
_FOLDER_HARD_MB = 300
_FOLDER_MAX_DEPTH = 3
_FOLDER_WORKERS = 4


def _clean_extensions(raw) -> set[str]:
    values = raw if isinstance(raw, list) else ([raw] if raw else [])
    return {"." + str(v).strip().lower().lstrip(".") for v in values if str(v).strip()}


def _collect_folder_files(
    base: str, folder: str, token: str, recursive: bool, depth: int, relative: str, found: list, errors: list
) -> None:
    items, error = _fetch_children(base, folder, token)
    if items is None:
        errors.append(f"{relative or '/'}: {str(error)[:80]}")
        return
    for item in items:
        name = str(item.get("name") or "")
        if "folder" in item:
            if recursive and depth < _FOLDER_MAX_DEPTH and name:
                child_relative = f"{relative}/{name}" if relative else name
                parent_drive = (item.get("parentReference") or {}).get("driveId")
                if folder == "" and base.startswith("/drives/") and item.get("id") and parent_drive:
                    child_base, child_folder = f"/drives/{parent_drive}/items/{item['id']}", ""
                else:
                    child_base, child_folder = base, f"{folder}/{name}" if folder else name
                _collect_folder_files(
                    child_base, child_folder, token, recursive, depth + 1, child_relative, found, errors
                )
        else:
            found.append({"item": item, "relative": relative})


def _download_folder_tool(args: dict, **_kwargs) -> str:
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, user_site_url, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)

    site_url = str(args.get("site_url") or user_site_url or "").strip().rstrip("/")
    folder_path = str(args.get("folder_path") or "").strip().strip("/")
    item_id = str(args.get("item_id") or "").strip()
    drive_id = str(args.get("drive_id") or "").strip()
    extensions = _clean_extensions(args.get("extensions"))
    recursive = bool(args.get("recursive"))
    try:
        max_files = max(1, min(_FOLDER_HARD_FILES, int(args.get("max_files") or _FOLDER_DEFAULT_FILES)))
        max_bytes = max(1, min(_FOLDER_HARD_MB, int(args.get("max_mb") or _FOLDER_DEFAULT_MB))) * 1024 * 1024
    except (TypeError, ValueError):
        max_files, max_bytes = _FOLDER_DEFAULT_FILES, _FOLDER_DEFAULT_MB * 1024 * 1024

    if item_id:
        label = item_id
        item_base = f"/drives/{drive_id}/items/{item_id}" if drive_id else f"/me/drive/items/{item_id}"
        candidates = [(item_base, "", None)]
    elif folder_path:
        label = folder_path
        if not site_url:
            folder_path, folder_error = _normalize_drive_folder(folder_path)
            if folder_error:
                return json.dumps({"success": False, "error": folder_error}, ensure_ascii=False)
        candidates = _site_bases(site_url, folder_path, token) if site_url else [("/me/drive/root", folder_path, None)]
    else:
        return json.dumps({"success": False, "error": "Indica folder_path (o item_id de la carpeta)."})

    found: list = []
    errors: list = []
    last_error = "No se pudo leer la carpeta."
    for base, folder, _name in candidates:
        found, errors = [], []
        probe, probe_error = _fetch_children(base, folder, token)
        if probe is None:
            last_error = probe_error or last_error
            continue
        _collect_folder_files(base, folder, token, recursive, 1, "", found, errors)
        break
    else:
        return json.dumps({"success": False, "error": _with_hint(last_error)}, ensure_ascii=False)

    selected: list = []
    skipped: list = []
    total = 0
    for entry in found:
        item = entry["item"]
        name = str(item.get("name") or "archivo")
        size = int(item.get("size") or 0)
        ext = os.path.splitext(name.lower())[1]
        if extensions and ext not in extensions:
            continue
        if size > _MAX_DOWNLOAD_BYTES:
            skipped.append({"name": name, "motivo": f"pesa {size / (1024 * 1024):.1f} MB (máximo 20 MB por archivo)"})
        elif len(selected) >= max_files:
            skipped.append({"name": name, "motivo": f"se alcanzó el máximo de {max_files} archivos"})
        elif total + size > max_bytes:
            skipped.append(
                {"name": name, "motivo": f"se alcanzó el máximo de {max_bytes // (1024 * 1024)} MB en total"}
            )
        else:
            total += size
            selected.append(entry)

    slug = re.sub(r"[^\w.\- ]", "_", os.path.basename(str(label).rstrip("/"))).strip(" .") or "carpeta"
    destination = Path(os.getcwd()) / _DOWNLOAD_DIR / slug
    destination.mkdir(parents=True, exist_ok=True)

    def _fetch_one(entry: dict) -> dict:
        item = entry["item"]
        name = str(item.get("name") or "archivo")
        url = item.get("@microsoft.graph.downloadUrl")
        if url and token != BROKER_TOKEN:
            data, derr = _download_bytes(url)
        else:
            drive = (item.get("parentReference") or {}).get("driveId") or drive_id
            path = (
                f"/drives/{drive}/items/{item.get('id')}/content"
                if drive
                else f"/me/drive/items/{item.get('id')}/content"
            )
            data, derr = _download_bytes(f"{GRAPH_BASE}{path}", token)
        if data is None:
            return {"name": name, "error": derr}
        relative = entry["relative"]
        safe = re.sub(r"[^\w.\- ]", "_", name).strip(" .") or "archivo"
        prefix = re.sub(r"[^\w.\- ]", "_", relative.replace("/", "__")) + "__" if relative else ""
        target = destination / f"{prefix}{safe}"
        target.write_bytes(data)
        return {
            "name": name,
            "size": len(data),
            "local_path": f"{_DOWNLOAD_DIR}/{slug}/{target.name}",
            "subcarpeta": relative or None,
        }

    downloaded: list = []
    with ThreadPoolExecutor(max_workers=_FOLDER_WORKERS) as pool:
        for result in pool.map(_fetch_one, selected):
            if "error" in result:
                skipped.append({"name": result["name"], "motivo": result["error"]})
            else:
                downloaded.append(result)

    output = {
        "success": True,
        "carpeta": label,
        "descargados": len(downloaded),
        "archivos": downloaded,
        "omitidos": skipped,
        "bytes_totales": sum(f["size"] for f in downloaded),
        "hint": "Archivos en el workspace. Lee los de texto con read_file y analiza las imágenes con vision_analyze, de a una.",
    }
    if errors:
        output["subcarpetas_sin_leer"] = errors[:5]
    return json.dumps(output, ensure_ascii=False)


def _read_file_tool(args: dict, **_kwargs) -> str:
    try:
        max_chars = int(args.get("max_chars") or _DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = _DEFAULT_MAX_CHARS
    max_chars = max(500, min(max_chars, _HARD_MAX_CHARS))

    error, info = _fetch_item(args)
    if error:
        return error
    name, size, data = info["name"], info["size"], info["data"]

    if os.path.splitext(name.lower())[1] in _IMAGE_EXTENSIONS:
        return json.dumps(_local_file_result(info), ensure_ascii=False)

    text, terr, extra = _extract_text(name, data, max_chars)
    if text is None:
        return json.dumps({"success": False, "name": name, "size": size, "error": terr, **extra})

    truncated = len(text) > max_chars
    return json.dumps(
        {
            "success": True,
            "name": name,
            "size": size,
            "url": info["url"],
            "truncated": truncated,
            "chars": min(len(text), max_chars),
            "content": text[:max_chars],
            **extra,
        },
        ensure_ascii=False,
    )


_MAX_BATCH_ITEMS = {"queries": 6, "items_read": 5, "items_download": 8}
_BATCH_READ_CHARS = 12000


def _search_multi_tool(args: dict, **kwargs) -> str:
    queries = args.get("queries")
    if not isinstance(queries, list) or not queries:
        return _search_tool(args, **kwargs)
    searches = []
    for query in [str(q).strip() for q in queries if str(q or "").strip()][: _MAX_BATCH_ITEMS["queries"]]:
        outcome = json.loads(_search_tool({**args, "query": query, "queries": None}, **kwargs))
        searches.append({"consulta": query, "resultado": outcome})
    if not searches:
        return json.dumps({"success": False, "error": "Se requiere query o queries (lista de términos)."})
    return json.dumps(
        {"success": any(x["resultado"].get("success") for x in searches), "busquedas": searches}, ensure_ascii=False
    )


def _batch_items(args: dict, limit_key: str):
    items = args.get("items")
    if not isinstance(items, list) or not items:
        return None
    return [item for item in items if isinstance(item, dict)][: _MAX_BATCH_ITEMS[limit_key]]


def _read_multi_tool(args: dict, **kwargs) -> str:
    items = _batch_items(args, "items_read")
    if items is None:
        return _read_file_tool(args, **kwargs)
    files = []
    for item in items:
        merged = {
            "max_chars": _BATCH_READ_CHARS,
            **{k: v for k, v in args.items() if k not in ("items", "max_chars")},
            **item,
        }
        files.append(json.loads(_read_file_tool(merged, **kwargs)))
    return json.dumps({"success": any(f.get("success") for f in files), "archivos": files}, ensure_ascii=False)


def _download_multi_tool(args: dict, **kwargs) -> str:
    items = _batch_items(args, "items_download")
    if items is None:
        return _download_file_tool(args, **kwargs)
    files = []
    for item in items:
        merged = {**{k: v for k, v in args.items() if k != "items"}, **item}
        files.append(json.loads(_download_file_tool(merged, **kwargs)))
    return json.dumps({"success": any(f.get("success") for f in files), "archivos": files}, ensure_ascii=False)


def register(ctx):
    ctx.register_tool(
        name="sharepoint_list_drives",
        toolset=TOOLSET,
        schema={
            "name": "sharepoint_list_drives",
            "description": "Lista las unidades de almacenamiento accesibles: OneDrive personal y bibliotecas de documentos de SharePoint.",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_list_drives_tool,
        description="Listar drives disponibles (OneDrive + SharePoint)",
        emoji="🗄️",
    )
    ctx.register_tool(
        name="sharepoint_list_files",
        toolset=TOOLSET,
        schema={
            "name": "sharepoint_list_files",
            "description": (
                "Lista archivos y carpetas en OneDrive o en un sitio SharePoint. Con site_url y folder_path prueba solo las "
                "bibliotecas del sitio hasta encontrar la carpeta (no hace falta adivinar el nombre de la biblioteca). "
                "Con depth=2 o 3 devuelve el ÁRBOL de subcarpetas en una sola llamada: úsalo para mapear o entender cómo "
                "está estructurada una carpeta, en vez de buscar con sharepoint_search una y otra vez. Para «mostrar todas "
                "las carpetas a las que tengo acceso» llama UNA vez con solo_carpetas=true y depth=4 (devuelve solo carpetas, "
                "con cuántos archivos tiene cada una); NO llames carpeta por carpeta."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "site_url": {
                        "type": "string",
                        "description": "URL del sitio SharePoint (opcional, ej: https://empresa.sharepoint.com/sites/Marketing). Si no se indica, usa el OneDrive del usuario.",
                    },
                    "folder_path": {
                        "type": "string",
                        "description": "Ruta de carpeta (opcional, ej: 'Gerencia/TI/10. SGSI' o 'Documentos/Proyectos')",
                    },
                    "item_id": {
                        "type": "string",
                        "description": "ID de la carpeta (de sharepoint_search): lista su contenido directamente, sin adivinar la ruta. Alternativa a folder_path.",
                    },
                    "drive_id": {
                        "type": "string",
                        "description": "ID de la unidad (opcional, junto con item_id).",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Niveles de subcarpetas a devolver como árbol (1 a 3, default 1; hasta 4 con solo_carpetas)",
                    },
                    "solo_carpetas": {
                        "type": "boolean",
                        "description": "true: el árbol trae solo carpetas (con el número de archivos de cada una), mucho más corto; ideal para mapear todo.",
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "Máximo de elementos del árbol (50 a 800, default 300).",
                    },
                },
            },
        },
        handler=_list_files_tool,
        description="Listar archivos en SharePoint/OneDrive",
        emoji="📁",
    )
    ctx.register_tool(
        name="sharepoint_get_file",
        toolset=TOOLSET,
        schema={
            "name": "sharepoint_get_file",
            "description": "Obtiene información de un archivo por su ruta en OneDrive o SharePoint.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Ruta del archivo (ej: 'Documents/informe.pdf')"},
                    "site_url": {"type": "string", "description": "URL del sitio SharePoint (opcional)"},
                },
                "required": ["file_path"],
            },
        },
        handler=_get_file_tool,
        description="Obtener archivo de SharePoint/OneDrive",
        emoji="📄",
    )
    ctx.register_tool(
        name="sharepoint_search",
        toolset=TOOLSET,
        schema={
            "name": "sharepoint_search",
            "description": (
                "Busca archivos y documentos en SharePoint y OneDrive usando Microsoft Search. Cada resultado trae "
                "item_id y drive_id: para leer o descargar un resultado pásalos a sharepoint_read_file o "
                "sharepoint_download_file en vez de adivinar la ruta o el site_url."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Término de búsqueda"},
                    "queries": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Varios términos en UNA llamada (hasta 6), en vez de buscar uno por uno.",
                    },
                    "count": {"type": "integer", "description": "Número de resultados (máx 25, default 10)"},
                },
                "required": [],
            },
        },
        handler=_search_multi_tool,
        description="Buscar archivos en SharePoint/OneDrive",
        emoji="🔍",
    )
    ctx.register_tool(
        name="sharepoint_read_file",
        toolset=TOOLSET,
        schema={
            "name": "sharepoint_read_file",
            "description": (
                "Lee el CONTENIDO de texto de un archivo de OneDrive o SharePoint "
                "(PDF, Word .docx, Excel .xlsx, PowerPoint .pptx, txt, csv, md, json, html). "
                "Si el archivo es una imagen, la guarda en el workspace y devuelve local_path para analizarla "
                "con vision_analyze. Usa esta herramienta cuando el usuario pida saber qué dice o contiene un "
                "archivo. Máximo 20 MB; el texto se recorta a max_chars."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Ruta del archivo (ej: 'Documentos/informe.pdf'). Alternativa a item_id.",
                    },
                    "item_id": {
                        "type": "string",
                        "description": "ID del archivo (de sharepoint_search o sharepoint_list_files). Alternativa a file_path.",
                    },
                    "drive_id": {"type": "string", "description": "ID de la unidad (opcional, junto con item_id)"},
                    "site_url": {"type": "string", "description": "URL del sitio SharePoint (opcional)"},
                    "max_chars": {
                        "type": "integer",
                        "description": "Máximo de caracteres a devolver (default 20000, tope 100000)",
                    },
                    "items": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": (
                            "Varios archivos en UNA llamada (hasta 5): [{item_id, drive_id} o {file_path}]. "
                            "Cada uno se recorta a 12000 caracteres salvo que indiques max_chars."
                        ),
                    },
                },
            },
        },
        handler=_read_multi_tool,
        description="Leer contenido de un archivo de SharePoint/OneDrive",
        emoji="📖",
    )
    ctx.register_tool(
        name="sharepoint_share_file",
        toolset=TOOLSET,
        schema={
            "name": "sharepoint_share_file",
            "description": (
                "Sube un archivo del directorio de trabajo del agente (PDF, PPTX, imagen, etc.; máx. 50 MB) al OneDrive "
                "del usuario (carpeta UltraBot-archivos) y devuelve un ENLACE para abrirlo o compartirlo. Úsala cuando "
                "el usuario pida el link de un archivo generado o cuando no pueda visualizarlo en el chat. IMPORTANTE: "
                "solo SUBE UNA COPIA y crea el enlace; NO se lo envía ni da acceso a ninguna persona en particular. Si "
                "el usuario pide compartir con alguien, después del enlace envíaselo tú con vista previa y aprobación: "
                "por Teams (teams_send_to_person con el enlace en message, o con file_path para adjuntar el archivo) o "
                "por correo (ms365_mail_send con el enlace o el adjunto). Nunca digas «compartido con X» solo por haber "
                "creado el enlace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Ruta del archivo dentro del directorio de trabajo (ej. outputs/informe.pdf)",
                    },
                    "name": {"type": "string", "description": "Nombre con que se guarda (opcional)"},
                    "scope": {
                        "type": "string",
                        "enum": ["organization", "anonymous"],
                        "description": (
                            "organization (default): solo personas de la organización. anonymous: cualquiera con el "
                            "enlace; úsalo SOLO si el usuario pide compartir con externos y su organización lo permite."
                        ),
                    },
                    "aprobacion_id": {
                        "type": "string",
                        "description": "Id de la aprobación del usuario para crear el enlace (lo devuelve la primera llamada).",
                    },
                    "aprobado_por_el_usuario": {
                        "type": "boolean",
                        "description": "true solo cuando el usuario ya confirmó, en un mensaje posterior, la vista previa.",
                    },
                },
                "required": ["file_path"],
            },
        },
        handler=_share_file_tool,
        description="Subir archivo a OneDrive y obtener enlace",
        emoji="🔗",
    )
    ctx.register_tool(
        name="sharepoint_upload_file",
        toolset=TOOLSET,
        schema={
            "name": "sharepoint_upload_file",
            "description": (
                "Guarda un archivo del directorio de trabajo del agente (máx. 50 MB) en una carpeta del OneDrive "
                "del propio usuario, por ejemplo Escritorio/Proyectos/IA. Acepta una ruta de Windows como "
                "C:\\Users\\x\\OneDrive - Empresa\\Escritorio\\Proyectos (se traduce sola). Crea las carpetas "
                "que falten y NUNCA sobrescribe: si ya existe un archivo con ese nombre, lo guarda con otro. No "
                "renombra ni mueve: subir con otro nombre crea una copia y el archivo anterior sigue en su carpeta. "
                "Si el OneDrive está sincronizado, el archivo aparece en el equipo del usuario. Úsala cuando el "
                "usuario pida guardar o dejar un archivo en una carpeta suya; para solo un enlace usa sharepoint_share_file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Ruta del archivo dentro del directorio de trabajo (ej. outputs/informe.docx)",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Carpeta de destino en el OneDrive del usuario (ej. Escritorio/Ana/Proyectos/IA) o su ruta de Windows",
                    },
                    "name": {"type": "string", "description": "Nombre con que se guarda (opcional)"},
                    "site_url": {
                        "type": "string",
                        "description": "Solo para guardar en una biblioteca de un SITIO de SharePoint (no en el OneDrive): "
                        "dirección del sitio (https://empresa.sharepoint.com/sites/TI). 'folder' empieza por el nombre de la "
                        "biblioteca (o la etiqueta «Sitio - Biblioteca» que muestra OneDrive). Si el usuario solo dio la "
                        "ruta sincronizada y el agente tiene un sitio asignado, se usa ese.",
                    },
                },
                "required": ["file_path", "folder"],
            },
        },
        handler=_upload_file_tool,
        description="Guardar archivo en una carpeta del OneDrive del usuario",
        emoji="📤",
    )
    ctx.register_tool(
        name="sharepoint_download_file",
        toolset=TOOLSET,
        schema={
            "name": "sharepoint_download_file",
            "description": (
                "Descarga un archivo de OneDrive o SharePoint al workspace del agente y devuelve su local_path. "
                "Úsala para imágenes (luego vision_analyze con local_path) o para procesar el archivo con código. "
                "No uses la URL web de SharePoint con otras herramientas: pide inicio de sesión. Máximo 20 MB."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Ruta del archivo (ej: 'Documentos/foto.png'). Alternativa a item_id.",
                    },
                    "item_id": {"type": "string", "description": "ID del archivo. Alternativa a file_path."},
                    "drive_id": {"type": "string", "description": "ID de la unidad (opcional, junto con item_id)"},
                    "site_url": {"type": "string", "description": "URL del sitio SharePoint (opcional)"},
                    "items": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Varios archivos en UNA llamada (hasta 8): [{item_id, drive_id} o {file_path}].",
                    },
                },
            },
        },
        handler=_download_multi_tool,
        description="Descargar archivo de SharePoint/OneDrive al workspace",
        emoji="⬇️",
    )
    ctx.register_tool(
        name="sharepoint_download_folder",
        toolset=TOOLSET,
        schema={
            "name": "sharepoint_download_folder",
            "description": (
                "Descarga TODOS los archivos de una carpeta de OneDrive o SharePoint al workspace en UNA llamada "
                "(en paralelo) y devuelve la lista con su local_path. Úsala cuando necesites varios archivos de una "
                "carpeta (una biblioteca de referencia, un lote de imágenes o documentos): NO llames a "
                "sharepoint_search y sharepoint_download_file archivo por archivo. Filtra por extensiones y, con "
                "recursive=true, entra en subcarpetas (hasta 3 niveles). Máximo 100 archivos y 300 MB por llamada; "
                "cada archivo hasta 20 MB. Los que no caben salen en 'omitidos' con su motivo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_path": {
                        "type": "string",
                        "description": "Ruta de la carpeta (ej: 'Escritorio/Proyectos/IA/Referencias'). Alternativa a item_id.",
                    },
                    "site_url": {"type": "string", "description": "URL del sitio SharePoint (opcional)"},
                    "item_id": {
                        "type": "string",
                        "description": "ID de la carpeta (de sharepoint_search). Alternativa a folder_path.",
                    },
                    "drive_id": {"type": "string", "description": "ID de la unidad (opcional, junto con item_id)"},
                    "extensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Solo estas extensiones, ej. ['pptx','png']. Sin indicar, baja todo.",
                    },
                    "recursive": {"type": "boolean", "description": "true: incluye subcarpetas (hasta 3 niveles)."},
                    "max_files": {"type": "integer", "description": "Máximo de archivos (default 40, tope 100)."},
                    "max_mb": {"type": "integer", "description": "Máximo de MB en total (default 150, tope 300)."},
                },
            },
        },
        handler=_download_folder_tool,
        description="Descargar una carpeta completa de SharePoint/OneDrive al workspace",
        emoji="📥",
    )
