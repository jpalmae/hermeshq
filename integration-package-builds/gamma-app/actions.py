from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://public-api.gamma.app/v1.0"


def _base_url(config: dict) -> str:
    return str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")


async def run_action(action_slug: str, *, agent, config: dict, resolve_secret, workspaces_root, package_root=None):
    if action_slug not in {"list_themes", "list_folders"}:
        return False, f"Unknown action: {action_slug}", None

    secret_ref = str(config.get("api_key_ref") or "").strip()
    if not secret_ref:
        return False, "Gamma API key secret is not configured.", None

    api_key = resolve_secret(secret_ref)
    if asyncio.iscoroutine(api_key):
        api_key = await api_key
    if not api_key:
        return False, "Configured Gamma API key secret could not be resolved.", None

    endpoint = "/themes" if action_slug == "list_themes" else "/folders"
    result = _get_json(
        f"{_base_url(config)}{endpoint}?{urllib.parse.urlencode({'limit': 25})}",
        api_key,
    )
    if not result["success"]:
        return False, result["message"], result.get("details")

    payload = result["data"] or {}
    items = payload.get("themes") if action_slug == "list_themes" else payload.get("folders")
    items = items if isinstance(items, list) else []
    normalized = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
        }
        for item in items
        if isinstance(item, dict)
    ]
    noun = "themes" if action_slug == "list_themes" else "folders"
    return True, f"Gamma returned {len(normalized)} {noun}.", {"items": normalized, "count": len(normalized)}


def _get_json(url: str, api_key: str) -> dict:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Content-Type": "application/json",
            "X-API-KEY": api_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
            return {"success": True, "data": payload}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"success": False, "message": f"Gamma API returned {exc.code}.", "details": {"body": body[:4000]}}
    except Exception as exc:
        return {"success": False, "message": f"Gamma API request failed: {exc}", "details": None}
