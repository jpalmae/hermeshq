from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from hermeshq.config import get_settings
from hermeshq.models.agent import Agent
from hermeshq.services.managed_capabilities import get_managed_integration


class ManagedIntegrationActionError(RuntimeError):
    pass


async def run_managed_integration_action(
    agent: Agent,
    integration_slug: str,
    action_slug: str,
    config: dict[str, str] | None,
    enabled_integration_slugs: list[str],
    resolve_secret,
) -> tuple[bool, str, dict | None]:
    integration = get_managed_integration(integration_slug, enabled_integration_slugs, include_uninstalled=True)
    if not integration:
        raise ManagedIntegrationActionError("Managed integration not found")

    action = next((item for item in (integration.get("actions") or []) if item.get("slug") == action_slug), None)
    if not action:
        raise ManagedIntegrationActionError("Managed integration action not found")

    merged = {
        **{key: str(value) for key, value in (integration.get("defaults") or {}).items()},
        **{
            key: str(value)
            for key, value in ((agent.integration_configs or {}).get(integration_slug) or {}).items()
            if isinstance(key, str)
        },
        **{key: str(value) for key, value in (config or {}).items() if isinstance(key, str)},
    }

    module = _load_actions_module(integration)
    if not module:
        raise ManagedIntegrationActionError("No actions module is defined for this integration")
    run_action = getattr(module, "run_action", None)
    if not callable(run_action):
        raise ManagedIntegrationActionError("Managed integration actions module is missing run_action()")

    result = run_action(
        action_slug,
        agent=agent,
        config=merged,
        resolve_secret=resolve_secret,
        workspaces_root=get_settings().workspaces_root,
        package_root=integration.get("package_root"),
    )
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, tuple) or len(result) != 3:
        raise ManagedIntegrationActionError("Managed integration action returned an invalid result")
    success, message, details = result
    return bool(success), str(message), details if isinstance(details, dict) or details is None else {"result": details}


def _load_actions_module(integration: dict) -> ModuleType | None:
    package_root = integration.get("package_root")
    if not package_root:
        return None
    relative_path = str(integration.get("actions_path") or "actions.py").strip()
    if not relative_path:
        return None
    module_path = Path(package_root) / relative_path
    if not module_path.exists():
        return None

    spec = importlib.util.spec_from_file_location(
        f"hermeshq_integration_actions_{integration['slug']}_{uuid4().hex}",
        module_path,
    )
    if not spec or not spec.loader:
        raise ManagedIntegrationActionError("Could not load managed integration actions")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
