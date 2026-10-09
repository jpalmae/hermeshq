"""Fetch available models from a provider's API."""

import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.app_settings import AppSettings
from hermeshq.models.provider import ProviderDefinition
from hermeshq.models.secret import Secret
from hermeshq.services.secret_vault import SecretVault

logger = logging.getLogger(__name__)

MODELS_CACHE_TTL_SECONDS = 3600


def _can_fetch_models(runtime_provider: str) -> bool:
    return True


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


async def _resolve_api_key(
    db: AsyncSession,
    provider: ProviderDefinition,
    settings: AppSettings | None,
    vault: SecretVault,
) -> str | None:
    from hermeshq.models.agent import Agent

    def _decrypt(secret_row):
        try:
            return vault.decrypt(secret_row.value_enc)
        except Exception:
            logger.warning("Failed to decrypt secret '%s'", secret_row.name)
            return None

    # 1. Provider-specific API key (set in Settings > Providers)
    if provider.api_key_ref:
        secret = await db.get(Secret, provider.api_key_ref)
        if secret:
            key = _decrypt(secret)
            if key:
                return key

    # 2. Global default API key
    default_ref = settings.default_api_key_ref if settings else None
    if default_ref:
        secret = await db.get(Secret, default_ref)
        if secret:
            key = _decrypt(secret)
            if key:
                return key

    # 3. Secret associated with this provider (Secret.provider == slug)
    result = await db.execute(select(Secret).where(Secret.provider == provider.slug))
    for secret in result.scalars().all():
        key = _decrypt(secret)
        if key:
            return key

    # 4. Any agent using this provider — use its api_key_ref
    result = await db.execute(
        select(Agent.api_key_ref)
        .where(
            Agent.provider == provider.runtime_provider,
            Agent.api_key_ref.isnot(None),
        )
        .limit(1)
    )
    agent_ref = result.scalar_one_or_none()
    if agent_ref:
        secret = await db.get(Secret, agent_ref)
        if secret:
            key = _decrypt(secret)
            if key:
                return key

    return None


async def _fetch_openai_models(base_url: str, api_key: str) -> list[str]:
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
    models = []
    for item in data.get("data", []):
        model_id = item.get("id")
        if model_id:
            models.append(model_id)
    return sorted(set(models))


async def _fetch_anthropic_models(base_url: str, api_key: str) -> list[str]:
    url = base_url.rstrip("/") + "/models"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
    models = []
    for item in data.get("data", []):
        model_id = item.get("id")
        if model_id:
            models.append(model_id)
    return sorted(set(models))


async def _fetch_gemini_models(api_key: str) -> list[str]:
    url = f"{GEMINI_BASE}/models?key={api_key}&pageSize=100"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
    models = []
    for item in data.get("models", []):
        name = item.get("name", "")
        if name.startswith("models/"):
            name = name[len("models/") :]
        if name:
            models.append(name)
    return sorted(set(models))


async def refresh_provider_models(
    db: AsyncSession,
    provider: ProviderDefinition,
    settings: AppSettings | None,
    vault: SecretVault,
) -> list[str]:
    if not _can_fetch_models(provider.runtime_provider):
        raise ValueError(f"Provider '{provider.slug}' does not support dynamic model fetching")

    api_key = await _resolve_api_key(db, provider, settings, vault)
    if not api_key:
        raise ValueError("No API key configured. Set a default_api_key_ref in Settings > Defaults.")

    base_url = provider.base_url
    if not base_url:
        raise ValueError(f"Provider '{provider.slug}' has no base_url configured")

    try:
        if provider.runtime_provider == "gemini-api":
            models = await _fetch_gemini_models(api_key)
        elif provider.runtime_provider in ("anthropic", "anthropic-api"):
            models = await _fetch_anthropic_models(base_url, api_key)
        else:
            models = await _fetch_openai_models(base_url, api_key)
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"API returned {exc.response.status_code}: {exc.response.text[:200]}") from exc
    except httpx.HTTPError as exc:
        raise ValueError(f"Network error: {exc}") from exc

    if not models:
        raise ValueError("API returned no models")

    provider.available_models = models
    provider.models_refreshed_at = datetime.now(UTC)
    await db.commit()

    logger.info("Refreshed %d models for provider '%s'", len(models), provider.slug)
    return models


def is_cache_fresh(provider: ProviderDefinition) -> bool:
    if not provider.models_refreshed_at:
        return False
    age = (datetime.now(UTC) - provider.models_refreshed_at.replace(tzinfo=UTC)).total_seconds()
    return age < MODELS_CACHE_TTL_SECONDS
