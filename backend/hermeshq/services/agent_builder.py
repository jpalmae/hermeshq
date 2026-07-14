"""AI Agent Builder service.

Conversational orchestrator that uses an LLM with tool-calling to help
users create agents. Includes deterministic connector gating.
"""

import json
import logging
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.app_settings import AppSettings
from hermeshq.schemas.agent_builder import (
    AgentDraft,
    AgentBuilderTurn,
    RequiredConnector,
)
from hermeshq.services.managed_capabilities import list_available_integration_packages

logger = logging.getLogger(__name__)

BUILDER_SYSTEM_PROMPT = """You are HermesHQ's AI Agent Builder assistant. You help users create AI agents by understanding their needs and proposing agent configurations.

CRITICAL RULES:
1. The user is NOT technical. Never ask about programming, tools, libraries, APIs, or implementation details. You are the expert — decide those details yourself.
2. Be proactive. Propose a complete draft in your FIRST response whenever possible. Only ask clarifying questions if the request is truly ambiguous.
3. Respond in the user's language (default Spanish).
4. Keep responses concise. Use Markdown formatting (bold, lists, tables) for readability.
5. NEVER output <tool_call> blocks in your natural language response. The tool call is parsed separately — your visible text should be a clean, natural explanation for the user.

PLATFORM CONTEXT — HermesHQ is a multi-agent platform with these capabilities:
- Messaging channels: telegram, whatsapp, microsoft_teams, google_chat, sixagentic (mobile app). The user may refer to any of these by name.
- Agents can browse the web, read/write files, execute code (technical profile), and use integrations.
- Agents can be scheduled to run at specific times (cron schedules).
- Agents can generate documents (PDF, images, reports) using their tools.

RUNTIME PROFILES (you decide which one — never ask the user):
- standard: Web browsing, file read/write, memory, vision, messaging, PDF generation. Good for research, summaries, notifications.
- technical: Adds code execution, git, and terminal access. Use when the agent needs to run scripts, generate PDFs, or do data processing.
- security: Adds security scanning and network tools.

INTEGRATION PACKAGES:
- Use list_capabilities to see what connectors are available (SharePoint, M365 Mail, Google Workspace, etc.).
- When the user mentions a service (email, calendar, documents), map it to the appropriate integration slug.
- "sixagentic" is the platform's mobile app channel — it's always available, no integration needed.

WORKFLOW:
1. Understand what the user wants in plain language.
2. Immediately propose a complete draft: friendly_name, description, system_prompt (detailed and professional), runtime_profile, integration_configs.
3. Use propose_agent_draft with ready_to_create=true in your first response if the request is clear enough.
4. The user can then accept or request changes.

When proposing integration_configs, use the slug as the key (e.g., {"sharepoint": {}, "ms365-mail": {}}).
Write system_prompts in Spanish unless the user speaks another language. Make them detailed and specific to the agent's purpose.

AVAILABLE TOOLS — call them using this EXACT format:

<tool_call>
<function=propose_agent_draft>
<parameter=friendly_name>Agent Display Name</parameter>
<parameter=description>What the agent does</parameter>
<parameter=system_prompt>Detailed agent instructions in Spanish</parameter>
<parameter=runtime_profile>standard</parameter>
<parameter=integration_configs>{"slug-name": {}}</parameter>
<parameter=ready_to_create>true</parameter>
</function>
</tool_call>

You can also call:
<tool_call>
<function=list_capabilities>
</function>
</tool_call>

IMPORTANT: Write a natural language message for the user FIRST, then add the tool call at the end. The tool call block will be parsed and executed automatically — the user will only see your natural language text.
"""


class BuilderSession:
    def __init__(self, session_id: str, tool_mode: str = "native"):
        self.session_id = session_id
        self.tool_mode = tool_mode
        self.history: list[dict[str, str]] = []
        self.draft: AgentDraft = AgentDraft()
        self.created_at: float = time.time()
        self.llm_messages: list[dict] = [
            {"role": "system", "content": BUILDER_SYSTEM_PROMPT}
        ]

    def is_expired(self, ttl_seconds: int = 1800) -> bool:
        return (time.time() - self.created_at) > ttl_seconds


_sessions: dict[str, BuilderSession] = {}


def create_builder_session(tool_mode: str = "native") -> BuilderSession:
    session_id = uuid.uuid4().hex[:16]
    session = BuilderSession(session_id, tool_mode)
    _sessions[session_id] = session
    return session


def get_builder_session(session_id: str) -> BuilderSession | None:
    session = _sessions.get(session_id)
    if session and session.is_expired():
        _sessions.pop(session_id, None)
        return None
    return session


def purge_expired_sessions() -> int:
    expired = [sid for sid, s in _sessions.items() if s.is_expired()]
    for sid in expired:
        _sessions.pop(sid, None)
    return len(expired)


def _get_builder_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_capabilities",
                "description": "List all available integration packages and their status (installed/not installed).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_runtime_profiles",
                "description": "List available runtime profiles and their default toolsets.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_agent_draft",
                "description": "Propose or update the agent draft. Call this when you have enough information to create a complete agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Agent identifier (kebab-case)"},
                        "friendly_name": {"type": "string", "description": "Display name"},
                        "description": {"type": "string", "description": "What the agent does"},
                        "system_prompt": {"type": "string", "description": "Instructions for the agent"},
                        "runtime_profile": {"type": "string", "enum": ["standard", "technical", "security"]},
                        "integration_configs": {
                            "type": "object",
                            "description": "Map of integration slug to config dict",
                        },
                        "ready_to_create": {"type": "boolean", "description": "True when the draft is complete and user confirmed"},
                    },
                    "required": ["friendly_name", "system_prompt", "runtime_profile"],
                },
            },
        },
    ]


async def _execute_tool(
    tool_name: str,
    arguments: dict,
    session: BuilderSession,
    db: AsyncSession,
) -> str:
    if tool_name == "list_capabilities":
        settings = await db.get(AppSettings, "default")
        enabled = settings.enabled_integration_packages if settings else []
        packages = list_available_integration_packages(enabled)
        summary = [
            {
                "slug": p["slug"],
                "name": p.get("name", p["slug"]),
                "description": p.get("description", ""),
                "installed": p.get("installed", False),
                "required_fields": p.get("required_fields", []),
            }
            for p in packages
        ]
        return json.dumps(summary)

    if tool_name == "list_runtime_profiles":
        return json.dumps([
            {"slug": "standard", "description": "General-purpose agent with safe tools, browser, file, memory, vision"},
            {"slug": "technical", "description": "Adds code execution, git, and terminal access"},
            {"slug": "security", "description": "Adds security scanning and network tools"},
        ])

    if tool_name == "propose_agent_draft":
        draft_data = {k: v for k, v in arguments.items() if k != "ready_to_create"}
        for key, val in draft_data.items():
            if hasattr(session.draft, key):
                setattr(session.draft, key, val)
        ready = arguments.get("ready_to_create", False)
        return json.dumps({"status": "updated", "ready_to_create": ready})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


def _compute_required_connectors(
    draft: AgentDraft,
    enabled_slugs: list[str],
) -> list[RequiredConnector]:
    if not draft.integration_configs:
        return []

    enabled_set = set(enabled_slugs)
    all_packages = list_available_integration_packages(enabled_slugs)
    pkg_by_slug = {p["slug"]: p for p in all_packages}

    result: list[RequiredConnector] = []
    for slug in draft.integration_configs:
        pkg = pkg_by_slug.get(slug)
        if not pkg:
            continue

        installed = slug in enabled_set
        required_fields = pkg.get("required_fields", [])
        pkg_name = pkg.get("name", slug)

        if installed:
            admin_instructions = ""
        else:
            fields_str = ", ".join(required_fields) if required_fields else "sin campos adicionales"
            admin_instructions = (
                f"Pide a tu administrador que habilite '{pkg_name}' en "
                f"Ajustes → Integraciones y configure: {fields_str}."
            )

        result.append(RequiredConnector(
            slug=slug,
            name=pkg_name,
            installed=installed,
            required_fields=required_fields,
            admin_instructions=admin_instructions,
        ))

    return result


async def resolve_builder_llm(
    db: AsyncSession,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the LLM provider and model for the builder.

    Returns (api_key, model, base_url) or (None, None, None) if unavailable.
    """
    from hermeshq.models.secret import Secret
    from hermeshq.services.secret_vault import SecretVault
    from hermeshq.config import get_settings
    from sqlalchemy import select

    settings = get_settings()
    app_settings = await db.get(AppSettings, "default")

    model = None
    api_key = None
    base_url = None

    if app_settings:
        model = app_settings.default_model
        base_url = app_settings.default_base_url
        if app_settings.default_api_key_ref:
            try:
                result = await db.execute(
                    select(Secret).where(Secret.name == app_settings.default_api_key_ref)
                )
                secret = result.scalar_one_or_none()
                if secret:
                    vault = SecretVault(settings.jwt_secret)
                    api_key = vault.decrypt(secret.value_enc)
            except Exception:
                logger.warning("Failed to resolve builder API key", exc_info=True)

    if not base_url:
        base_url = "https://api.openai.com/v1"

    return api_key, model, base_url


def _extract_inline_tool_calls(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Extract tool calls from LLM text output (XML-like format).

    Returns list of (function_name, arguments_dict).
    """
    import re

    calls: list[tuple[str, dict[str, Any]]] = []

    for block in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.DOTALL):
        inner = block.group(1)
        func_match = re.search(r"<function=([\w_]+)>", inner)
        if not func_match:
            continue

        func_name = func_match.group(1)
        params: dict[str, Any] = {}

        for param_match in re.finditer(r"<parameter=([\w_]+)>\s*(.*?)\s*</parameter>", inner, re.DOTALL):
            key = param_match.group(1)
            val = param_match.group(2).strip()
            if val.lower() in ("true", "false"):
                params[key] = val.lower() == "true"
            elif val.startswith("{") or val.startswith("["):
                try:
                    params[key] = json.loads(val)
                except Exception:
                    params[key] = val
            else:
                params[key] = val

        calls.append((func_name, params))

    return calls


def _strip_tool_call_blocks(text: str) -> str:
    """Remove <tool_call>...</tool_call> blocks and any leftover tool-call artifacts."""
    import re

    # Remove closed <tool_call>...</tool_call> blocks
    clean = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL)
    # Remove unclosed <tool_call> blocks (model forgot to close)
    clean = re.sub(r"<tool_call>.*", "", clean, flags=re.DOTALL)
    # Remove stray <parameter=name>content</parameter> blocks (content included)
    clean = re.sub(r"<parameter=\w+>.*?</parameter>", "", clean, flags=re.DOTALL)
    # Remove any stray XML-like tags the model might emit
    clean = re.sub(r"</?function=\w+>", "", clean)
    clean = re.sub(r"</?parameter=\w+>", "", clean)
    # Clean up excessive whitespace
    clean = clean.strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean


async def process_builder_message(
    session: BuilderSession,
    user_text: str,
    db: AsyncSession,
) -> AgentBuilderTurn:
    """Process a user message and return a builder turn.

    This calls the LLM with tools, executes tool calls, and returns
    the assistant response + updated draft.
    """
    import openai

    api_key, model, base_url = await resolve_builder_llm(db)

    if not api_key or not model:
        return AgentBuilderTurn(
            assistant_text="No hay un modelo de IA configurado. Contacta al administrador para configurar un proveedor en Ajustes.",
            draft=session.draft,
            required_connectors=[],
            ready_to_create=False,
        )

    session.llm_messages.append({"role": "user", "content": user_text})

    MAX_LLM_MESSAGES = 60
    if len(session.llm_messages) > MAX_LLM_MESSAGES:
        session.llm_messages = [session.llm_messages[0]] + session.llm_messages[-(MAX_LLM_MESSAGES - 1):]

    client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=session.llm_messages,
            temperature=0.7,
            max_tokens=2000,
            timeout=60,
        )

        if not response.choices:
            raise ValueError("LLM returned no choices")

        raw_text = response.choices[0].message.content or ""

        # Parse and execute inline tool calls
        parsed_calls = _extract_inline_tool_calls(raw_text)
        ready_flag = False

        if parsed_calls:
            clean_text = _strip_tool_call_blocks(raw_text)
            session.llm_messages.append({"role": "assistant", "content": clean_text})

            for tc_name, tc_args in parsed_calls:
                result = await _execute_tool(tc_name, tc_args, session, db)
                if tc_name == "propose_agent_draft":
                    parsed = json.loads(result)
                    ready_flag = parsed.get("ready_to_create", False)
                session.llm_messages.append({
                    "role": "user",
                    "content": f"[Tool result for {tc_name}]: {result}",
                })

            # Follow-up for natural language response
            follow_up = await client.chat.completions.create(
                model=model,
                messages=session.llm_messages,
                temperature=0.7,
                max_tokens=2000,
                timeout=60,
            )
            if follow_up.choices:
                assistant_text = follow_up.choices[0].message.content or ""
            else:
                assistant_text = clean_text
        else:
            assistant_text = raw_text

        # FINAL SAFETY: strip any remaining tool call blocks
        assistant_text = _strip_tool_call_blocks(assistant_text)

        if not assistant_text and parsed_calls:
            assistant_text = "He preparado el borrador del agente. Revisa el panel lateral para ver los detalles."

        session.llm_messages.append({"role": "assistant", "content": assistant_text})

    except Exception:
        logger.error("Builder LLM call failed", exc_info=True)
        return AgentBuilderTurn(
            assistant_text="Lo siento, hubo un error al procesar tu mensaje. Por favor intenta de nuevo.",
            draft=session.draft,
            required_connectors=[],
            ready_to_create=False,
        )

    settings = await db.get(AppSettings, "default")
    enabled_slugs = settings.enabled_integration_packages if settings else []
    required = _compute_required_connectors(session.draft, enabled_slugs)

    return AgentBuilderTurn(
        assistant_text=assistant_text,
        draft=session.draft,
        required_connectors=required,
        ready_to_create=ready_flag
        or (session.draft.friendly_name is not None
            and session.draft.system_prompt is not None),
    )


async def finalize_agent_from_draft(
    session: BuilderSession,
    db: AsyncSession,
    app_state: object,
    created_by_user_id: str = "",
) -> tuple[str, str]:
    """Create an agent from the builder draft.

    Returns (agent_id, agent_name).
    Raises on validation errors.
    """
    from hermeshq.schemas.agent import AgentCreate
    from hermeshq.services.agent_factory import create_agent_from_config
    from sqlalchemy import select
    from hermeshq.models.node import Node

    draft = session.draft
    agent_name = draft.name or draft.friendly_name or "ai-agent"
    if not draft.friendly_name or not draft.system_prompt:
        raise ValueError("Draft is incomplete: friendly_name and system_prompt are required")

    node_result = await db.execute(select(Node).limit(1))
    node = node_result.scalar_one_or_none()
    if not node:
        raise ValueError("No node available for agent creation")

    payload = AgentCreate(
        node_id=node.id,
        name=agent_name,
        friendly_name=draft.friendly_name,
        slug=draft.slug or agent_name.lower().replace(" ", "-"),
        description=draft.description,
        runtime_profile=draft.runtime_profile,
        system_prompt=draft.system_prompt,
        enabled_toolsets=draft.enabled_toolsets,
        integration_configs=draft.integration_configs,
    )

    workspace_manager = getattr(app_state, "workspace_manager", None)
    hermes_version_manager = getattr(app_state, "hermes_version_manager", None)

    if not workspace_manager or not hermes_version_manager:
        raise ValueError("Server is not fully initialized. Please try again.")

    agent = await create_agent_from_config(
        db=db,
        payload=payload,
        workspace_manager=workspace_manager,
        hermes_version_manager=hermes_version_manager,
    )

    try:
        from hermeshq.services.audit import record_audit
        await record_audit(
            db,
            actor_id=created_by_user_id,
            action="agent.created_via_builder",
            target_type="agent",
            target_id=str(agent.id),
            target_name=str(agent.name),
            details={"draft": draft.model_dump()},
        )
    except Exception:
        logger.warning("Failed to record audit for builder agent creation", exc_info=True)

    try:
        supervisor = getattr(app_state, "supervisor", None)
        if supervisor:
            await supervisor.start_agent(str(agent.id))
            logger.info("Agent %s started after builder creation", agent.id)
    except Exception:
        logger.warning("Failed to auto-start agent %s after builder creation", agent.id, exc_info=True)

    return str(agent.id), str(agent.name)
