from __future__ import annotations

from hermeshq.services.managed_capabilities import CORE_MANAGED_PLUGIN_CATALOG
from hermeshq.services.runtime_profiles import list_runtime_profiles, terminal_allowed_for_profile

TOOLSET_CATALOG: dict[str, dict[str, str]] = {
    "safe": {"name": "Safe", "description": "Safe default toolset intended for low-risk runtime operations."},
    "browser": {"name": "Browser", "description": "Web browsing and page interaction tools."},
    "file": {"name": "File", "description": "Workspace file read and write tools."},
    "skills": {"name": "Skills", "description": "Installed skill loading and usage support."},
    "memory": {"name": "Memory", "description": "Longer-lived memory tools across sessions."},
    "session_search": {"name": "Session search", "description": "Session history search and retrieval tools."},
    "todo": {"name": "Todo", "description": "Task planning and checklist style tools."},
    "clarify": {"name": "Clarify", "description": "Clarification workflow tools for ambiguous requests."},
    "cronjob": {"name": "Cronjob", "description": "Scheduling and recurring execution helpers."},
    "messaging": {"name": "Messaging", "description": "Messaging and channel interaction helpers."},
    "delegation": {"name": "Delegation", "description": "Delegation helpers for multi-agent workflows."},
}

PLATFORM_PLUGIN_CATALOG: dict[str, dict[str, str]] = {
    "hermeshq_comms": {
        "name": "HermesHQ Comms",
        "description": "Built-in HermesHQ plugin exposing inter-agent roster, direct messaging, and delegation tools.",
    },
}


def build_runtime_capability_overview() -> dict:
    profiles_payload: list[dict] = []
    for profile in list_runtime_profiles():
        toolsets = [_serialize_toolset(slug) for slug in profile["defaults"].get("enabled_toolsets") or []]
        profiles_payload.append(
            {
                "slug": profile["slug"],
                "name": profile["name"],
                "description": profile["description"],
                "tooling_summary": profile["tooling_summary"],
                "container_intent": profile["container_intent"],
                "terminal_allowed": terminal_allowed_for_profile(profile["slug"]),
                "phase1_full_access": not toolsets and terminal_allowed_for_profile(profile["slug"]),
                "builtin_toolsets": toolsets,
            }
        )

    platform_plugins_payload = []
    for item in CORE_MANAGED_PLUGIN_CATALOG:
        slug = str(item["slug"])
        meta = PLATFORM_PLUGIN_CATALOG.get(slug, {})
        platform_plugins_payload.append(
            {
                "slug": slug,
                "name": meta.get("name") or slug,
                "toolset": str(item["toolset"]),
                "description": meta.get("description") or "Built-in HermesHQ platform plugin.",
                "standard_compatible": bool(item.get("standard_compatible")),
            }
        )

    return {
        "profiles": profiles_payload,
        "platform_plugins": platform_plugins_payload,
    }


def _serialize_toolset(slug: str) -> dict:
    meta = TOOLSET_CATALOG.get(slug, {})
    return {
        "slug": slug,
        "name": meta.get("name") or slug.replace("_", " ").title(),
        "description": meta.get("description") or "Runtime toolset available by default for this profile.",
    }
