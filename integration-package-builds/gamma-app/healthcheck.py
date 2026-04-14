from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://public-api.gamma.app/v1.0"


def _base_url(config: dict) -> str:
    return str(config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")


async def test_connection(config: dict, resolve_secret):
    secret_ref = str(config.get("api_key_ref") or "").strip()
    if not secret_ref:
        return False, "Gamma API key secret is not configured.", None

    api_key = resolve_secret(secret_ref)
    if asyncio.iscoroutine(api_key):
        api_key = await api_key
    if not api_key:
        return False, "Configured Gamma API key secret could not be resolved.", None

    url = f"{_base_url(config)}/themes?{urllib.parse.urlencode({'limit': 1})}"
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
            themes = payload.get("themes") or []
            return True, "Gamma API connection succeeded.", {"theme_count_sample": len(themes), "base_url": _base_url(config)}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, f"Gamma API returned {exc.code}.", {"body": body[:4000], "base_url": _base_url(config)}
    except Exception as exc:
        return False, f"Gamma API connection failed: {exc}", {"base_url": _base_url(config)}
