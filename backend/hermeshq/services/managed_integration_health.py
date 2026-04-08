from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from hermeshq.models.agent import Agent
from hermeshq.services.managed_capabilities import get_managed_integration


class ManagedIntegrationTestError(RuntimeError):
    pass


async def test_managed_integration(
    agent: Agent,
    integration_slug: str,
    config: dict[str, str] | None,
    enabled_integration_slugs: list[str],
    resolve_secret,
) -> tuple[bool, str, dict | None]:
    integration = get_managed_integration(integration_slug, enabled_integration_slugs, include_uninstalled=True)
    if not integration:
        raise ManagedIntegrationTestError("Managed integration not found")

    merged = {
        **{key: str(value) for key, value in (integration.get("defaults") or {}).items()},
        **{
            key: str(value)
            for key, value in ((agent.integration_configs or {}).get(integration_slug) or {}).items()
            if isinstance(key, str)
        },
        **{key: str(value) for key, value in (config or {}).items() if isinstance(key, str)},
    }

    module = _load_healthcheck_module(integration)
    if not module:
        raise ManagedIntegrationTestError("No health test is defined for this integration")
    test_connection = getattr(module, "test_connection", None)
    if not callable(test_connection):
        raise ManagedIntegrationTestError("Managed integration healthcheck is missing test_connection()")

    result = test_connection(config=merged, resolve_secret=resolve_secret)
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
