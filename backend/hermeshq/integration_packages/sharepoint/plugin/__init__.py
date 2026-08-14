from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOOLSET = "hermeshq_sharepoint"


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


def _get_m365_token(user_id: str) -> tuple[str | None, str | None, str]:
    """Returns (access_token, sharepoint_site_url, error_detail)."""
    base_url = os.environ.get("HERMESHQ_INTERNAL_API_URL", "").rstrip("/")
    agent_id = os.environ.get("HERMESHQ_AGENT_ID", "")
    agent_token = os.environ.get("HERMESHQ_AGENT_TOKEN", "")
    if not base_url or not agent_id or not agent_token:
        return None, None, "HermesHQ internal control no configurado"
    url = f"{base_url}/control/m365/agent-token?user_id={user_id}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"X-HermesHQ-Agent-ID": agent_id, "X-HermesHQ-Agent-Token": agent_token},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            site_url = (data.get("sharepoint_site_url") or "").strip().rstrip("/") or None
            return data.get("access_token"), site_url, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail", body)
        except (json.JSONDecodeError, ValueError):
            detail = body
        return None, None, str(detail)
    except Exception as exc:  # noqa: BLE001  # HTTP request catch-all
        return None, None, str(exc)


def _graph(method: str, path: str, access_token: str, payload: dict | None = None) -> dict:
    url = (path if path.startswith("https://") else f"{GRAPH_BASE}{path}").replace(" ", "%20")
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


def _list_files_tool(args: dict, **_kwargs) -> str:
    """List files using Files.Read.All - works with OneDrive and SharePoint."""
    user_id = _task_user_id()
    if not user_id:
        return json.dumps({"success": False, "error": "No se pudo determinar el usuario de esta tarea."})
    token, user_site_url, err = _get_m365_token(user_id)
    if not token:
        return _auth_error(err)

    # Use arg site_url, fallback to user-configured site, fallback to OneDrive
    site_url = str(args.get("site_url") or user_site_url or "").strip().rstrip("/")
    folder_path = str(args.get("folder_path") or "").strip().strip("/")

    if site_url:
        # Access a specific SharePoint site by URL
        # GET /sites/{hostname}:/{site-path}
        parsed = urllib.parse.urlparse(site_url)
        hostname = parsed.netloc
        site_path = parsed.path.rstrip("/") or "/"
        base_path = f"/sites/{hostname}:{site_path}:/drive/root"
    else:
        # Default: user's OneDrive root
        base_path = "/me/drive/root"

    path: str | None = f"{base_path}:/{folder_path}:/children" if folder_path else f"{base_path}/children"

    raw_items: list = []
    while path and len(raw_items) < 200:
        result = _graph("GET", path, token)
        if "error" in result:
            return json.dumps({"success": False, "error": result["error"]})
        raw_items.extend(result.get("value", []))
        next_link: str = result.get("@odata.nextLink") or ""
        path = next_link if (next_link and len(raw_items) < 200) else None

    simplified = [
        {
            "id": i.get("id"),
            "name": i.get("name"),
            "type": "folder" if "folder" in i else "file",
            "size": i.get("size"),
            "url": i.get("webUrl"),
            "modified": i.get("lastModifiedDateTime"),
        }
        for i in raw_items
    ]
    return json.dumps({"success": True, "count": len(simplified), "items": simplified}, ensure_ascii=False)


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
        return json.dumps({"success": False, "error": result["error"]})
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
    result = _graph("POST", "/search/query", token, payload)
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
                    }
                )
    return json.dumps({"success": True, "count": len(hits), "hits": hits}, ensure_ascii=False)


# ── Plugin registration ───────────────────────────────────────────────────────


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
            "description": "Lista archivos y carpetas en OneDrive o en un sitio SharePoint específico.",
            "parameters": {
                "type": "object",
                "properties": {
                    "site_url": {
                        "type": "string",
                        "description": "URL del sitio SharePoint (opcional, ej: https://empresa.sharepoint.com/sites/Marketing). Si no se indica, usa el OneDrive del usuario.",
                    },
                    "folder_path": {
                        "type": "string",
                        "description": "Ruta de carpeta (opcional, ej: 'Documents/Projects')",
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
            "description": "Busca archivos y documentos en SharePoint y OneDrive usando Microsoft Search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Término de búsqueda"},
                    "count": {"type": "integer", "description": "Número de resultados (máx 25, default 10)"},
                },
                "required": ["query"],
            },
        },
        handler=_search_tool,
        description="Buscar archivos en SharePoint/OneDrive",
        emoji="🔍",
    )
