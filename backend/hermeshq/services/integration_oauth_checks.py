from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable


async def resolve_secret_value(resolve_secret, secret_ref: str) -> str | None:
    value = resolve_secret(secret_ref)
    if asyncio.iscoroutine(value):
        value = await value
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def post_form(url: str, payload: dict[str, str], headers: dict[str, str] | None = None) -> tuple[int, dict]:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")
        return response.status, json.loads(body) if body else {}


def get_json(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, method="GET", headers=headers or {})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")
        return response.status, json.loads(body) if body else {}


async def test_microsoft_graph_credentials(
    *,
    config: dict[str, str],
    resolve_secret,
    resource_path: str,
) -> tuple[bool, str, dict | None]:
    tenant_id = str(config.get("tenant_id") or "").strip()
    client_id = str(config.get("client_id") or "").strip()
    client_secret_ref = str(config.get("client_secret_ref") or "").strip()
    if not tenant_id or not client_id or not client_secret_ref:
        return False, "Tenant ID, client ID and client secret are required.", None

    client_secret = await resolve_secret_value(resolve_secret, client_secret_ref)
    if not client_secret:
        return False, "Configured Microsoft client secret could not be resolved.", None

    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    try:
        _, token_payload = post_form(
            token_url,
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
    except urllib.error.HTTPError as exc:
        return False, "Microsoft token request failed.", _decode_http_error(exc)
    except urllib.error.URLError as exc:
        return False, f"Microsoft token request could not be completed: {exc.reason}", None
    access_token = str(token_payload.get("access_token") or "").strip()
    if not access_token:
        return False, "Microsoft token endpoint did not return an access token.", {"token_response": token_payload}

    try:
        status_code, api_payload = get_json(
            f"https://graph.microsoft.com/v1.0/{resource_path.lstrip('/')}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except urllib.error.HTTPError as exc:
        return False, "Microsoft Graph verification request failed.", _decode_http_error(exc)
    except urllib.error.URLError as exc:
        return False, f"Microsoft Graph verification request could not be completed: {exc.reason}", None
    return True, "Microsoft Graph connection test passed.", {"status_code": status_code, "resource": resource_path, "response": api_payload}


async def test_google_oauth_credentials(
    *,
    config: dict[str, str],
    resolve_secret,
    resource_url: str,
) -> tuple[bool, str, dict | None]:
    client_id = str(config.get("client_id") or "").strip()
    client_secret_ref = str(config.get("client_secret_ref") or "").strip()
    refresh_token_ref = str(config.get("refresh_token_ref") or "").strip()
    if not client_id or not client_secret_ref or not refresh_token_ref:
        return False, "Client ID, client secret and refresh token are required.", None

    client_secret = await resolve_secret_value(resolve_secret, client_secret_ref)
    if not client_secret:
        return False, "Configured Google client secret could not be resolved.", None
    refresh_token = await resolve_secret_value(resolve_secret, refresh_token_ref)
    if not refresh_token:
        return False, "Configured Google refresh token could not be resolved.", None

    try:
        _, token_payload = post_form(
            "https://oauth2.googleapis.com/token",
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
    except urllib.error.HTTPError as exc:
        return False, "Google token request failed.", _decode_http_error(exc)
    except urllib.error.URLError as exc:
        return False, f"Google token request could not be completed: {exc.reason}", None
    access_token = str(token_payload.get("access_token") or "").strip()
    if not access_token:
        return False, "Google token endpoint did not return an access token.", {"token_response": token_payload}

    try:
        status_code, api_payload = get_json(
            resource_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except urllib.error.HTTPError as exc:
        return False, "Google Workspace verification request failed.", _decode_http_error(exc)
    except urllib.error.URLError as exc:
        return False, f"Google Workspace verification request could not be completed: {exc.reason}", None
    return True, "Google Workspace connection test passed.", {"status_code": status_code, "resource": resource_url, "response": api_payload}


def parse_sharepoint_site_url(site_url: str) -> tuple[str, str] | None:
    parsed = urllib.parse.urlparse(site_url)
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path.strip("/")
    if not path:
        return None
    return parsed.netloc, path


def render_required_fields(fields: Iterable[str]) -> str:
    return ", ".join(sorted(set(field for field in fields if field)))


def _decode_http_error(exc: urllib.error.HTTPError) -> dict:
    body = exc.read().decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(body) if body else {}
    except Exception:
        parsed = {"raw": body} if body else {}
    return {
        "status_code": exc.code,
        "response": parsed,
    }
