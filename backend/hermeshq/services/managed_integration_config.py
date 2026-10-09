from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from hermeshq.models.agent import Agent

SecretResolver = Callable[[str], str | None | Awaitable[str | None]]


def build_trusted_integration_config(
    agent: Agent,
    integration_slug: str,
    integration: dict,
) -> dict[str, str] | None:
    configs = agent.integration_configs or {}
    if integration_slug not in configs:
        return None
    stored = configs.get(integration_slug)
    stored_config = stored if isinstance(stored, dict) else {}
    return {
        **{key: str(value) for key, value in (integration.get("defaults") or {}).items()},
        **{key: str(value) for key, value in stored_config.items() if isinstance(key, str)},
    }


def build_scoped_secret_resolver(
    integration: dict,
    trusted_config: dict[str, str],
    resolve_secret: SecretResolver,
) -> SecretResolver:
    secret_fields = {
        str(field.get("name"))
        for field in (integration.get("fields") or [])
        if isinstance(field, dict) and field.get("kind") == "secret_ref" and field.get("name")
    }
    secret_fields.update(key for key in trusted_config if key.endswith("_ref"))
    allowed_refs = {
        str(trusted_config.get(field_name) or "").strip()
        for field_name in secret_fields
        if str(trusted_config.get(field_name) or "").strip()
    }

    async def _resolve(secret_ref: str) -> str | None:
        if secret_ref not in allowed_refs:
            return None
        value = resolve_secret(secret_ref)
        if inspect.isawaitable(value):
            return await value
        return value

    return _resolve


def validate_action_arguments(integration: dict, arguments: dict[str, object] | None) -> dict[str, object]:
    normalized = dict(arguments or {})
    protected_fields = {
        str(field.get("name"))
        for field in (integration.get("fields") or [])
        if isinstance(field, dict) and field.get("name")
    }
    protected_fields.update(str(key) for key in (integration.get("defaults") or {}))
    protected_fields.update(str(key) for key in (integration.get("env_map") or {}))
    invalid_fields = sorted(key for key in normalized if key in protected_fields or key.endswith("_ref"))
    if invalid_fields:
        raise ValueError(f"Action arguments cannot override integration configuration: {', '.join(invalid_fields)}")
    return normalized
