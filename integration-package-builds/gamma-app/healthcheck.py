from __future__ import annotations

import asyncio

import requests

DEFAULT_BASE_URL = "https://public-api.gamma.app/v1.0"
REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "HermesHQ-Gamma/1.0",
}


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

    try:
        response = requests.get(
            f"{_base_url(config)}/themes",
            params={"limit": 1},
            headers={**REQUEST_HEADERS, "X-API-KEY": api_key},
            timeout=30,
        )
        if response.status_code >= 400:
            return False, f"Gamma API returned {response.status_code}.", {"body": response.text[:4000], "base_url": _base_url(config)}
        payload = response.json() if response.text else {}
        themes = payload.get("data") or payload.get("themes") or []
        return True, "Gamma API connection succeeded.", {"theme_count_sample": len(themes), "base_url": _base_url(config)}
    except Exception as exc:
        return False, f"Gamma API connection failed: {exc}", {"base_url": _base_url(config)}
