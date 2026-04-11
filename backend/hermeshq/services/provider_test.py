from __future__ import annotations

import time

import httpx

from hermeshq.services.provider_catalog import BUILTIN_PROVIDERS


OPENAI_COMPATIBLE_PROVIDER_SLUGS = {
    "openai-api",
    "openrouter",
    "gemini-api",
    "deepseek-api",
    "xai-api",
    "alibaba-api",
    "huggingface",
    "minimax-api",
}
MODELS_PROVIDER_SLUGS = OPENAI_COMPATIBLE_PROVIDER_SLUGS | {"kimi-coding", "zai"}


def _get_builtin_provider(provider_slug: str) -> dict | None:
    return next((provider for provider in BUILTIN_PROVIDERS if provider["slug"] == provider_slug), None)


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _extract_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or f"Request failed with status {response.status_code}"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if isinstance(error, str) and error.strip():
            return error.strip()
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return f"Request failed with status {response.status_code}"


def _extract_model_ids(response: httpx.Response) -> list[str] | None:
    try:
        payload = response.json()
    except ValueError:
        return None

    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if not isinstance(data, list):
        return None

    model_ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id:
            model_ids.append(model_id)
        if len(model_ids) >= 10:
            break
    return model_ids or None


async def test_provider_credential(provider_slug: str, api_key: str, base_url: str | None) -> dict:
    builtin_provider = _get_builtin_provider(provider_slug)
    if not builtin_provider:
        return {
            "success": False,
            "status_code": None,
            "error": "Provider not found",
            "latency_ms": 0.0,
            "models_detected": None,
        }

    effective_base_url = _normalize_base_url(base_url or builtin_provider.get("base_url") or "")
    if not effective_base_url:
        return {
            "success": False,
            "status_code": None,
            "error": "Provider base URL is not configured",
            "latency_ms": 0.0,
            "models_detected": None,
        }

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if provider_slug == "anthropic-api":
                response = await client.post(
                    f"{effective_base_url}/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-5",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                )
                latency_ms = round((time.perf_counter() - start) * 1000, 2)
                return {
                    "success": response.status_code == 200,
                    "status_code": response.status_code,
                    "error": None if response.status_code == 200 else _extract_error_message(response),
                    "latency_ms": latency_ms,
                    "models_detected": None,
                }

            if provider_slug in MODELS_PROVIDER_SLUGS:
                response = await client.get(
                    f"{effective_base_url}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                latency_ms = round((time.perf_counter() - start) * 1000, 2)
                return {
                    "success": response.status_code == 200,
                    "status_code": response.status_code,
                    "error": None if response.status_code == 200 else _extract_error_message(response),
                    "latency_ms": latency_ms,
                    "models_detected": _extract_model_ids(response) if response.status_code == 200 else None,
                }

            return {
                "success": False,
                "status_code": None,
                "error": "Provider testing is not implemented",
                "latency_ms": round((time.perf_counter() - start) * 1000, 2),
                "models_detected": None,
            }
    except httpx.TimeoutException:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return {
            "success": False,
            "status_code": None,
            "error": "Request timed out",
            "latency_ms": latency_ms,
            "models_detected": None,
        }
    except httpx.HTTPError as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        return {
            "success": False,
            "status_code": None,
            "error": str(exc),
            "latency_ms": latency_ms,
            "models_detected": None,
        }
