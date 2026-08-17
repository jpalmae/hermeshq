from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from hermeshq.config import get_settings
from hermeshq.models.agent import Agent
from hermeshq.services.managed_capabilities import get_managed_integration
from hermeshq.services.managed_integration_config import (
    build_scoped_secret_resolver,
    build_trusted_integration_config,
)


class ManagedIntegrationTestError(RuntimeError):
    pass


async def test_managed_integration(
    agent: Agent,
    integration_slug: str,
    enabled_integration_slugs: list[str],
    resolve_secret,
) -> tuple[bool, str, dict | None]:
    integration = get_managed_integration(integration_slug, enabled_integration_slugs)
    if not integration:
        raise ManagedIntegrationTestError("Managed integration not found")

    trusted_config = build_trusted_integration_config(agent, integration_slug, integration)
    if trusted_config is None:
        raise ManagedIntegrationTestError("Managed integration is not enabled for this agent")
    trusted_config["__workspaces_root"] = str(get_settings().workspaces_root)
    scoped_resolve_secret = build_scoped_secret_resolver(integration, trusted_config, resolve_secret)

    module = _load_healthcheck_module(integration)
    if not module:
        raise ManagedIntegrationTestError("No health test is defined for this integration")
    test_connection = getattr(module, "test_connection", None)
    if not callable(test_connection):
        raise ManagedIntegrationTestError("Managed integration healthcheck is missing test_connection()")

    result = test_connection(config=trusted_config, resolve_secret=scoped_resolve_secret)
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, tuple) or len(result) != 3:
        raise ManagedIntegrationTestError("Managed integration healthcheck returned an invalid result")
    success, message, details = result
    return bool(success), str(message), details if isinstance(details, dict) or details is None else {"result": details}


def _load_healthcheck_module(integration: dict) -> ModuleType | None:
    package_root = integration.get("package_root")
    if not package_root:
        return None
    relative_path = str(integration.get("healthcheck_path") or "healthcheck.py").strip()
    if not relative_path:
        return None
    module_path = Path(package_root) / relative_path
    if not module_path.exists():
        return None

    spec = importlib.util.spec_from_file_location(
        f"hermeshq_integration_health_{integration['slug']}_{uuid4().hex}",
        module_path,
    )
    if not spec or not spec.loader:
        raise ManagedIntegrationTestError("Could not load managed integration healthcheck")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
