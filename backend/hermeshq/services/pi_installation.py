from __future__ import annotations

import json
import textwrap
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.config import get_settings
from hermeshq.core.security import create_agent_service_token
from hermeshq.models.agent import Agent
from hermeshq.services.credentials import require_secret_value
from hermeshq.services.env_sanitize import build_safe_env
from hermeshq.services.secret_vault import SecretVault


class PiInstallationManager:
    def __init__(
        self,
        secret_vault: SecretVault,
        workspace_manager,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.secret_vault = secret_vault
        self.workspace_manager = workspace_manager
        self.session_factory = session_factory

    async def sync_agent_installation(self, agent: Agent) -> None:
        pi_home = self.workspace_manager.build_pi_config_path(agent.id)
        pi_home.mkdir(parents=True, exist_ok=True)
        (pi_home / "extensions").mkdir(exist_ok=True)
        (pi_home / "skills").mkdir(exist_ok=True)

        await self._resolve_api_key(agent)
        self._write_settings(agent, pi_home)
        self._write_models(agent, pi_home)
        self._write_security_extension(pi_home)
        self._write_integration_extension(agent, pi_home)
        self._sync_skills(agent, pi_home)
        self._remove_legacy_managed_config(agent.id)

    def _sync_skills(self, agent: Agent, pi_home: Path) -> None:
        """Bridge agent skills from the shared Hermes skills dir into .pi/skills/.

        Hermes materializes agent.skills under workspace/.hermes/skills/hermeshq-managed/.
        Pi discovers skills via skillsOverride in the runner (project trust is disabled),
        so a plain copy is enough.
        """
        import shutil

        workspace = self.workspace_manager.build_workspace_path(agent.id)
        hermes_skills = workspace / ".hermes" / "skills" / "hermeshq-managed"
        pi_skills = pi_home / "skills"
        pi_skills.mkdir(exist_ok=True)

        if not hermes_skills.exists():
            return

        for skill_dir in hermes_skills.iterdir():
            if not skill_dir.is_dir():
                continue
            if not (skill_dir / "SKILL.md").exists():
                continue
            target = pi_skills / skill_dir.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(skill_dir, target, ignore=shutil.ignore_patterns(".hermeshq-skill.json"))

    async def _resolve_api_key(self, agent: Agent) -> str | None:
        if not agent.api_key_ref or not self.session_factory:
            return None
        async with self.session_factory() as session:
            return await require_secret_value(session, self.secret_vault, agent.api_key_ref)

    def _write_settings(self, agent: Agent, pi_home: Path) -> None:
        config = agent.pi_config or {}
        settings = {
            "compaction": config.get("compaction", {"enabled": True, "threshold_tokens": 100000}),
            "defaultProjectTrust": "never",
            "retry": {"enabled": True, "maxRetries": 3},
        }
        (pi_home / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")

    def _write_models(self, agent: Agent, pi_home: Path) -> None:
        model_id = agent.model or "gpt-4o"
        base_url = agent.base_url or "https://api.openai.com/v1"
        provider_name = "nvidia" if "nvidia" in (agent.provider or "") else "openai"
        api_key_env = "NVIDIA_API_KEY" if provider_name == "nvidia" else "OPENAI_API_KEY"

        models = {
            "providers": {
                provider_name: {
                    "baseUrl": base_url,
                    "api": "openai-completions",
                    "apiKey": "$" + api_key_env,
                    "models": [
                        {
                            "id": model_id,
                            "name": model_id.split("/")[-1],
                            "reasoning": False,
                            "input": ["text"],
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                            "contextWindow": 128000,
                            "maxTokens": 4096,
                        }
                    ],
                }
            }
        }
        (pi_home / "models.json").write_text(json.dumps(models, indent=2), encoding="utf-8")

    def _write_security_extension(self, pi_home: Path) -> None:
        extension = textwrap.dedent("""\
            import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

            const API_URL = process.env.HERMESHQ_INTERNAL_API_URL ?? "";
            const AGENT_TOKEN = process.env.HERMESHQ_AGENT_TOKEN ?? "";
            const AGENT_ID = process.env.HERMESHQ_AGENT_ID ?? "";

            export default function (pi: ExtensionAPI) {
              pi.on("tool_call", async (event, _ctx) => {
                const decision = await evaluatePermission(event.toolName, event.input as Record<string, unknown>);
                if (!decision.allowed) return { block: true, reason: decision.reason };
              });
            }

            async function evaluatePermission(tool: string, input: Record<string, unknown>) {
              if (!API_URL || !AGENT_TOKEN || !AGENT_ID) {
                return { allowed: false, reason: "HermesHQ policy service is not configured" };
              }
              try {
                const res = await fetch(`${API_URL}/control/permissions/evaluate`, {
                  method: "POST",
                  headers: {
                    "Content-Type": "application/json",
                    "X-HermesHQ-Agent-ID": AGENT_ID,
                    "X-HermesHQ-Agent-Token": AGENT_TOKEN,
                  },
                  body: JSON.stringify({ tool, input }),
                  signal: AbortSignal.timeout(5000),
                });
                if (!res.ok) return { allowed: false, reason: `Policy service returned HTTP ${res.status}` };
                const data = await res.json() as { allowed?: boolean; reason?: string | null };
                if (data.allowed !== true) {
                  return { allowed: false, reason: data.reason ?? "Tool call denied by HermesHQ policy" };
                }
                return { allowed: true, reason: null };
              } catch {
                return { allowed: false, reason: "HermesHQ policy service is unavailable" };
              }
            }
            """)
        (pi_home / "extensions" / "hermeshq-security.ts").write_text(extension, encoding="utf-8")

    def _write_integration_extension(self, agent: Agent, pi_home: Path) -> None:
        integrations = agent.integration_configs or {}
        integrations_json = json.dumps({key: True for key in integrations})

        extension = textwrap.dedent(f"""\
            import type {{ ExtensionAPI }} from "@earendil-works/pi-coding-agent";
            import {{ Type }} from "typebox";

            const API_URL = process.env.HERMESHQ_INTERNAL_API_URL ?? "";
            const AGENT_TOKEN = process.env.HERMESHQ_AGENT_TOKEN ?? "";
            const AGENT_ID = process.env.HERMESHQ_AGENT_ID ?? "";

            async function callIntegration(integrationSlug: string, action: string, args: Record<string, unknown>) {{
              const res = await fetch(
                `${{API_URL}}/control/agents/${{AGENT_ID}}/integrations/${{integrationSlug}}/actions/${{action}}`,
                {{
                  method: "POST",
                  headers: {{
                    "Content-Type": "application/json",
                    "X-HermesHQ-Agent-ID": AGENT_ID,
                    "X-HermesHQ-Agent-Token": AGENT_TOKEN,
                  }},
                  body: JSON.stringify(args),
                }},
              );
              if (!res.ok) throw new Error(`Integration request failed with HTTP ${{res.status}}`);
              return res.json();
            }}

            const INTEGRATIONS = {integrations_json};

            export default function (pi: ExtensionAPI) {{
              for (const slug of Object.keys(INTEGRATIONS)) {{
                const toolName = `integration_${{slug}}`.replace(/-/g, "_");
                pi.registerTool({{
                  name: toolName,
                  label: slug,
                  description: `Call ${{slug}} integration action`,
                  parameters: Type.Object({{
                    action: Type.String({{ description: "Action name (e.g. send_mail, list_events)" }}),
                    args: Type.Record(Type.String(), Type.Any(), {{ description: "Action arguments", default: {{}} }}),
                  }}),
                  async execute(_id, params) {{
                    const result = await callIntegration(slug, params.action, params.args ?? {{}});
                    const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
                    return {{ content: [{{ type: "text", text }}], details: {{}} }};
                  }},
                }});
              }}
            }}
            """)
        (pi_home / "extensions" / "hermeshq-integrations.ts").write_text(extension, encoding="utf-8")

    async def build_process_env(self, agent: Agent) -> dict[str, str]:
        env = build_safe_env()
        pi_home = self.workspace_manager.build_pi_config_path(agent.id)
        env["HERMESHQ_AGENT_ID"] = agent.id
        env["HERMESHQ_AGENT_TOKEN"] = create_agent_service_token(agent.id, agent.service_token_version or 1)
        env["PI_AGENT_DIR"] = str(pi_home)
        env["PI_CODING_AGENT_DIR"] = str(pi_home)
        settings = get_settings()
        env["HERMESHQ_INTERNAL_API_URL"] = settings.internal_api_base_url.rstrip("/")

        if agent.api_key_ref and self.session_factory:
            async with self.session_factory() as session:
                api_key = await require_secret_value(session, self.secret_vault, agent.api_key_ref)
        else:
            api_key = None

        if api_key:
            provider = agent.provider or "openai"
            if provider.startswith("anthropic"):
                env["ANTHROPIC_API_KEY"] = api_key
            elif "nvidia" in provider:
                env["NVIDIA_API_KEY"] = api_key
                env["OPENAI_API_KEY"] = api_key
            else:
                env["OPENAI_API_KEY"] = api_key
                if agent.base_url:
                    env["OPENAI_BASE_URL"] = agent.base_url

        if agent.model:
            env["PI_MODEL"] = agent.model

        return env

    def _remove_legacy_managed_config(self, agent_id: str) -> None:
        legacy_home = self.workspace_manager.build_workspace_path(agent_id) / ".pi"
        for relative_path in (
            "extensions/hermeshq-security.ts",
            "extensions/hermeshq-integrations.ts",
            "settings.json",
            "models.json",
            "models-store.json",
            "auth.json",
        ):
            (legacy_home / relative_path).unlink(missing_ok=True)
        for relative_path in ("extensions", "skills", "sessions", "."):
            path = legacy_home if relative_path == "." else legacy_home / relative_path
            try:
                path.rmdir()
            except OSError:
                pass

    def compose_system_prompt(self, agent: Agent) -> str:
        parts = []
        if agent.system_prompt:
            parts.append(agent.system_prompt)
        parts.append(
            f"\nYou are running inside HermesHQ as agent '{agent.friendly_name or agent.name}' "
            f"(ID: {agent.id}). Use the provided tools to complete tasks."
        )
        return "\n".join(parts)
