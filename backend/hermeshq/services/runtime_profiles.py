from copy import deepcopy

from hermeshq.services.managed_capabilities import list_standard_compatible_toolsets

STANDARD_ENABLED_TOOLSETS = [
    "safe",
    "browser",
    "file",
    "skills",
    "memory",
    "session_search",
    "todo",
    "clarify",
    "cronjob",
    "messaging",
    "delegation",
] + list_standard_compatible_toolsets()


BUILTIN_RUNTIME_PROFILES: list[dict] = [
    {
        "slug": "standard",
        "name": "Standard",
        "description": (
            "General-purpose shared backend profile for administrative, coordination, and "
            "integration-focused agents."
        ),
        "typical_roles": [
            "Executive assistant",
            "Email and calendar operations",
            "MS365 / Google Workspace coordination",
            "Knowledge worker",
        ],
        "tooling_summary": (
            "Shared backend runtime without terminal/process tools. Suitable for coordination, "
            "research, browser-based workflows, files, messaging, and managed skills."
        ),
        "container_intent": "Future default image for general-purpose agents.",
        "defaults": {
            "enabled_toolsets": STANDARD_ENABLED_TOOLSETS,
            "disabled_toolsets": [],
            "max_iterations": 90,
            "auto_approve_cmds": False,
            "command_allowlist": [],
        },
    },
    {
        "slug": "technical",
        "name": "Technical",
        "description": (
            "Shared backend profile intended for infrastructure, systems, networking, and "
            "operations agents that need a more tool-heavy runtime later."
        ),
        "typical_roles": [
            "Sysadmin",
            "DevOps",
            "Network admin",
            "SRE",
        ],
        "tooling_summary": (
            "Shared backend runtime with full tool access in phase 1. Suitable for systems, "
            "networking, shell, and operational workflows."
        ),
        "container_intent": "Future technical image with broader shell and network tooling.",
        "defaults": {
            "enabled_toolsets": [],
            "disabled_toolsets": [],
            "max_iterations": 120,
            "auto_approve_cmds": False,
            "command_allowlist": [],
        },
    },
    {
        "slug": "security",
        "name": "Security",
        "description": (
            "Shared backend profile intended for audit, security review, and defensive analysis "
            "agents that will later move to a more specialized image."
        ),
        "typical_roles": [
            "Cybersecurity analyst",
            "Security auditor",
            "Threat analyst",
            "Compliance reviewer",
        ],
        "tooling_summary": (
            "Shared backend runtime with full tool access in phase 1, intended for audit, "
            "security review, and deeper inspection workflows."
        ),
        "container_intent": "Future security image with hardened defaults and specialized tooling.",
        "defaults": {
            "enabled_toolsets": [],
            "disabled_toolsets": [],
            "max_iterations": 140,
            "auto_approve_cmds": False,
            "command_allowlist": [],
        },
    },
]


_PROFILE_INDEX = {profile["slug"]: profile for profile in BUILTIN_RUNTIME_PROFILES}


def normalize_runtime_profile_slug(value: str | None) -> str:
    slug = (value or "").strip().lower()
    if slug in _PROFILE_INDEX:
        return slug
    return "standard"


def get_runtime_profile(value: str | None) -> dict:
    return deepcopy(_PROFILE_INDEX[normalize_runtime_profile_slug(value)])


def list_runtime_profiles() -> list[dict]:
    return [deepcopy(item) for item in BUILTIN_RUNTIME_PROFILES]


def resolve_effective_toolsets(
    profile_slug: str | None,
    enabled_toolsets: list[str] | None,
    disabled_toolsets: list[str] | None,
) -> tuple[list[str], list[str]]:
    profile = get_runtime_profile(profile_slug)
    defaults = profile["defaults"]
    enabled = list(dict.fromkeys([*(defaults["enabled_toolsets"] or []), *(enabled_toolsets or [])]))
    disabled = list(dict.fromkeys([*(defaults["disabled_toolsets"] or []), *(disabled_toolsets or [])]))
    return enabled, disabled


def terminal_allowed_for_profile(profile_slug: str | None) -> bool:
    return normalize_runtime_profile_slug(profile_slug) != "standard"
