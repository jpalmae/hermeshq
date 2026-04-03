import json
import os
import re
import shutil
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.secret import Secret
from hermeshq.services.secret_vault import SecretVault


class HermesInstallationError(RuntimeError):
    pass


class HermesInstallationManager:
    _DESC_RE = re.compile(r"^\s*description:\s*(.+?)\s*$", re.MULTILINE)

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_vault: SecretVault,
    ) -> None:
        self.session_factory = session_factory
        self.secret_vault = secret_vault

    def build_hermes_home(self, workspace_path: str) -> Path:
        return Path(workspace_path) / ".hermes"

    async def sync_agent_installation(self, agent: Agent) -> list[dict]:
        hermes_home = self.build_hermes_home(agent.workspace_path)
        self._ensure_home_dirs(hermes_home)
        installed_skills = await self._sync_managed_skills(agent, hermes_home)
        system_prompt = await self._build_system_prompt(agent, installed_skills)
        messaging_channels = await self._load_messaging_channels(agent.id)
        self._write_config(agent, hermes_home, system_prompt, messaging_channels)
        self._write_soul(agent, hermes_home)
        await self._sync_auth_store(agent, hermes_home)
        await self._sync_dotenv(agent, hermes_home, messaging_channels)
        return installed_skills

    async def build_process_env(self, agent: Agent) -> dict[str, str]:
        hermes_home = self.build_hermes_home(agent.workspace_path)
        env = {**os.environ, "HERMES_HOME": str(hermes_home), "TERM": "xterm-256color"}
        api_key = await self._resolve_api_key(agent.api_key_ref)
        if api_key:
            for env_name in self._provider_env_names(agent.provider):
                env[env_name] = api_key
        if agent.base_url:
            provider_base_url_env = self._provider_base_url_env_name(agent.provider)
            if provider_base_url_env:
                env[provider_base_url_env] = agent.base_url
            env["OPENAI_BASE_URL"] = agent.base_url
        return env

    async def build_gateway_env(self, agent: Agent, platform: str) -> dict[str, str]:
        env = await self.build_process_env(agent)
        for key, value in (await self._build_managed_env_map(agent, platform)).items():
            env[key] = value
        return env

    async def get_runtime_system_prompt(self, agent: Agent) -> str:
        installed = await self.list_installed_skills(agent)
        return await self._build_system_prompt(agent, installed)

    async def list_installed_skills(self, agent: Agent) -> list[dict]:
        hermes_home = self.build_hermes_home(agent.workspace_path)
        return self._scan_installed_skills(hermes_home)

    async def search_catalog(self, query: str, limit: int = 20) -> list[dict]:
        from tools.skills_hub import GitHubAuth, OptionalSkillSource, SkillsShSource

        results: list[dict] = []
        seen: set[str] = set()
        sources = [OptionalSkillSource(), SkillsShSource(GitHubAuth())]

        for source in sources:
            try:
                found = source.search(query, limit=limit)
            except Exception:
                continue
            for meta in found:
                if meta.identifier in seen:
                    continue
                seen.add(meta.identifier)
                results.append(
                    {
                        "name": meta.name,
                        "description": meta.description,
                        "identifier": meta.identifier,
                        "source": meta.source,
                        "trust_level": meta.trust_level,
                        "repo": meta.repo,
                        "path": meta.path,
                        "tags": meta.tags,
                        "extra": meta.extra,
                    }
                )
                if len(results) >= limit:
                    return results
        return results

    def _ensure_home_dirs(self, hermes_home: Path) -> None:
        hermes_home.mkdir(parents=True, exist_ok=True)
        for subdir in ("cron", "sessions", "logs", "memories", "skills", "plugins"):
            (hermes_home / subdir).mkdir(parents=True, exist_ok=True)

    def _write_config(
        self,
        agent: Agent,
        hermes_home: Path,
        system_prompt: str,
        messaging_channels: list[MessagingChannel],
    ) -> None:
        telegram_channel = next((item for item in messaging_channels if item.platform == "telegram"), None)
        config = {
            "model": {
                "default": agent.model,
                "provider": agent.provider,
                "base_url": agent.base_url or "",
            },
            "agent": {
                "max_turns": agent.max_iterations,
                "system_prompt": system_prompt,
            },
            "skills": {
                "external_dirs": [],
            },
        }
        if telegram_channel:
            platforms = config.setdefault("platforms", {})
            telegram_platform = {
                "enabled": bool(telegram_channel.enabled),
            }
            if telegram_channel.home_chat_id:
                telegram_platform["home_channel"] = {
                    "platform": "telegram",
                    "chat_id": telegram_channel.home_chat_id,
                    "name": telegram_channel.home_chat_name or "Home",
                }
            platforms["telegram"] = telegram_platform
            config["telegram"] = {
                "require_mention": bool(telegram_channel.require_mention),
                "free_response_chats": list(telegram_channel.free_response_chat_ids or []),
                "unauthorized_dm_behavior": telegram_channel.unauthorized_dm_behavior or "pair",
            }
        config_path = hermes_home / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def _write_soul(self, agent: Agent, hermes_home: Path) -> None:
        (hermes_home / "SOUL.md").write_text(
            agent.soul_md or "# Soul\n\nHermesHQ managed agent.",
            encoding="utf-8",
        )

    async def _sync_managed_skills(self, agent: Agent, hermes_home: Path) -> list[dict]:
        managed_root = hermes_home / "skills" / "hermeshq-managed"
        managed_root.mkdir(parents=True, exist_ok=True)

        desired_ids = [skill for skill in agent.skills if isinstance(skill, str) and skill.strip()]
        desired_names: set[str] = set()
        installed: list[dict] = []

        for identifier in desired_ids:
            cached = self._load_cached_skill(managed_root, identifier)
            if cached:
                desired_names.add(cached["name"])
                installed.append(cached)
                continue

            bundle = await self._fetch_skill_bundle(identifier)
            desired_names.add(bundle["name"])
            target_dir = managed_root / bundle["name"]
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            for rel_path, content in bundle["files"].items():
                file_path = target_dir / rel_path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    file_path.write_bytes(content)
                else:
                    file_path.write_text(content, encoding="utf-8")
            metadata = {
                "name": bundle["name"],
                "identifier": identifier,
                "description": self._extract_description(bundle["files"].get("SKILL.md", "")),
                "source": bundle["source"],
                "managed": True,
            }
            (target_dir / ".hermeshq-skill.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            installed.append(metadata)

        for path in managed_root.iterdir():
            if path.is_dir() and path.name not in desired_names:
                shutil.rmtree(path)

        return installed

    async def _fetch_skill_bundle(self, identifier: str) -> dict:
        from tools.skills_hub import GitHubAuth, OptionalSkillSource, SkillsShSource, WellKnownSkillSource

        identifier = identifier.strip()
        source = None
        if identifier.startswith("skills-sh/"):
            source = SkillsShSource(GitHubAuth())
        elif identifier.startswith("official/"):
            source = OptionalSkillSource()
        elif identifier.startswith("well-known/") or identifier.startswith("http://") or identifier.startswith("https://"):
            source = WellKnownSkillSource()
        else:
            source = SkillsShSource(GitHubAuth())

        bundle = source.fetch(identifier)
        if not bundle:
            raise HermesInstallationError(f"Skill '{identifier}' could not be fetched from its source")
        return {
            "name": bundle.name,
            "files": bundle.files,
            "source": bundle.source,
            "identifier": bundle.identifier,
            "trust_level": bundle.trust_level,
            "metadata": bundle.metadata,
        }

    def _scan_installed_skills(self, hermes_home: Path) -> list[dict]:
        skills_root = hermes_home / "skills"
        if not skills_root.exists():
            return []
        installed: list[dict] = []
        for skill_md in sorted(skills_root.rglob("SKILL.md")):
            try:
                content = skill_md.read_text(encoding="utf-8")
            except Exception:
                continue
            installed.append(
                {
                    "name": skill_md.parent.name,
                    "description": self._extract_description(content),
                    "path": str(skill_md.parent.relative_to(skills_root)),
                    "managed": "hermeshq-managed" in skill_md.parts,
                }
            )
        return installed

    def _load_cached_skill(self, managed_root: Path, identifier: str) -> dict | None:
        expected_name = identifier.strip().split("/")[-1]
        for skill_dir in managed_root.iterdir():
            if not skill_dir.is_dir():
                continue
            meta_path = skill_dir / ".hermeshq-skill.json"
            if meta_path.exists():
                try:
                    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    metadata = None
                if isinstance(metadata, dict) and metadata.get("identifier") == identifier:
                    return {
                        "name": metadata.get("name") or skill_dir.name,
                        "identifier": identifier,
                        "description": metadata.get("description") or self._extract_description((skill_dir / "SKILL.md").read_text(encoding="utf-8")),
                        "source": metadata.get("source") or "cached",
                        "managed": True,
                    }
            if skill_dir.name == expected_name and (skill_dir / "SKILL.md").exists():
                return {
                    "name": skill_dir.name,
                    "identifier": identifier,
                    "description": self._extract_description((skill_dir / "SKILL.md").read_text(encoding="utf-8")),
                    "source": "cached",
                    "managed": True,
                }
        return None

    def _extract_description(self, skill_md: str | bytes) -> str:
        if isinstance(skill_md, bytes):
            text = skill_md.decode("utf-8", errors="replace")
        else:
            text = skill_md
        match = self._DESC_RE.search(text)
        if match:
            return match.group(1).strip().strip("\"'")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
                return stripped[:200]
        return ""

    async def _build_system_prompt(self, agent: Agent, installed_skills: list[dict]) -> str:
        roster = await self._load_agent_roster(agent)
        return self._compose_system_prompt(agent, installed_skills, roster)

    async def _load_agent_roster(self, agent: Agent) -> list[dict]:
        async with self.session_factory() as session:
            result = await session.execute(select(Agent).order_by(Agent.created_at.asc()))
            agents = list(result.scalars().all())

        name_by_id = {
            item.id: (item.friendly_name or item.name or item.slug or item.id)
            for item in agents
        }
        roster: list[dict] = []
        for item in agents:
            roster.append(
                {
                    "id": item.id,
                    "self": item.id == agent.id,
                    "display_name": item.friendly_name or item.name or item.slug or item.id,
                    "technical_name": item.name or "",
                    "slug": item.slug or "",
                    "description": (item.description or "").strip(),
                    "status": item.status,
                    "can_receive_tasks": bool(item.can_receive_tasks),
                    "supervisor": name_by_id.get(item.supervisor_agent_id) if item.supervisor_agent_id else None,
                    "team_tags": list(item.team_tags or []),
                }
            )
        return roster

    def _compose_system_prompt(self, agent: Agent, installed_skills: list[dict], roster: list[dict]) -> str:
        parts = [agent.system_prompt.strip()] if agent.system_prompt and agent.system_prompt.strip() else []
        if installed_skills:
            lines = [
                "HermesHQ assigned skills are installed in your Hermes home.",
                "Use the real Hermes tools `skills_list` and `skill_view` to inspect them before relying on them.",
                "Assigned skills:",
            ]
            lines.extend(
                f"- {skill['name']}: {skill['description'] or 'No description'}"
                for skill in installed_skills
            )
            parts.append("\n".join(lines))
        else:
            parts.append(
                "If asked which skills are available, do not guess. Use `skills_list` to verify installed skills. "
                "If none are installed, say that no agent-specific skills are currently installed."
            )
        if roster:
            lines = [
                "HermesHQ live roster for this instance. This is factual control-plane data, not memory.",
                "If asked whether you know another agent, answer from this roster.",
                "Known agents:",
            ]
            for item in roster:
                role = item["description"] or "No description"
                supervisor = item["supervisor"] or "none"
                tag_text = ", ".join(item["team_tags"]) if item["team_tags"] else "none"
                lines.append(
                    f"- {item['display_name']} | slug={item['slug'] or 'unset'} | status={item['status']} | "
                    f"receives_tasks={'yes' if item['can_receive_tasks'] else 'no'} | supervisor={supervisor} | "
                    f"tags={tag_text} | role={role}"
                )
            parts.append("\n".join(lines))
        return "\n\n".join(part for part in parts if part).strip()

    async def _resolve_api_key(self, api_key_ref: str | None) -> str | None:
        if not api_key_ref:
            return None
        async with self.session_factory() as session:
            result = await session.execute(select(Secret).where(Secret.name == api_key_ref))
            secret = result.scalar_one_or_none()
        if not secret:
            raise HermesInstallationError(f"Secret '{api_key_ref}' was not found")
        return self.secret_vault.decrypt(secret.value_enc)

    async def _load_messaging_channels(self, agent_id: str) -> list[MessagingChannel]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(MessagingChannel)
                .where(MessagingChannel.agent_id == agent_id)
                .order_by(MessagingChannel.platform.asc())
            )
            return list(result.scalars().all())

    async def _sync_dotenv(
        self,
        agent: Agent,
        hermes_home: Path,
        messaging_channels: list[MessagingChannel],
    ) -> None:
        env_path = hermes_home / ".env"
        managed_env = await self._build_managed_env_map(agent)
        self._merge_env_file(env_path, managed_env)

    async def _build_managed_env_map(
        self,
        agent: Agent,
        platform: str | None = None,
    ) -> dict[str, str]:
        managed: dict[str, str] = {}

        api_key = await self._resolve_api_key(agent.api_key_ref)
        if api_key:
            for env_name in self._provider_env_names(agent.provider):
                managed[env_name] = api_key
        if agent.base_url:
            provider_base_url_env = self._provider_base_url_env_name(agent.provider)
            if provider_base_url_env:
                managed[provider_base_url_env] = agent.base_url
            managed["OPENAI_BASE_URL"] = agent.base_url

        channels = await self._load_messaging_channels(agent.id)
        for channel in channels:
            if platform and channel.platform != platform:
                continue
            if channel.platform == "telegram":
                token = await self._resolve_api_key(channel.secret_ref)
                if token:
                    managed["TELEGRAM_BOT_TOKEN"] = token
                if channel.allowed_user_ids:
                    managed["TELEGRAM_ALLOWED_USERS"] = ",".join(channel.allowed_user_ids)
                if channel.home_chat_id:
                    managed["TELEGRAM_HOME_CHANNEL"] = channel.home_chat_id
                if channel.home_chat_name:
                    managed["TELEGRAM_HOME_CHANNEL_NAME"] = channel.home_chat_name
                if channel.free_response_chat_ids:
                    managed["TELEGRAM_FREE_RESPONSE_CHATS"] = ",".join(channel.free_response_chat_ids)
                managed["TELEGRAM_REQUIRE_MENTION"] = "true" if channel.require_mention else "false"
                managed["WHATSAPP_ENABLED"] = "false"
        return managed

    def _merge_env_file(self, env_path: Path, managed_env: dict[str, str]) -> None:
        managed_keys = {
            "OPENAI_BASE_URL",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_USERS",
            "TELEGRAM_HOME_CHANNEL",
            "TELEGRAM_HOME_CHANNEL_NAME",
            "TELEGRAM_FREE_RESPONSE_CHATS",
            "TELEGRAM_REQUIRE_MENTION",
            "WHATSAPP_ENABLED",
            *self._provider_env_names("zai"),
            *self._provider_env_names("openrouter"),
            *self._provider_env_names("anthropic"),
            *self._provider_env_names("openai"),
        }
        provider_base_envs = {
            key
            for key in (
                self._provider_base_url_env_name("zai"),
                self._provider_base_url_env_name("openrouter"),
                self._provider_base_url_env_name("openai"),
            )
            if key
        }
        managed_keys.update(provider_base_envs)

        preserved_lines: list[str] = []
        if env_path.exists():
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    preserved_lines.append(raw_line)
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key not in managed_keys:
                    preserved_lines.append(raw_line)

        rendered = [
            *preserved_lines,
            *[f"{key}={self._format_env_value(value)}" for key, value in managed_env.items() if value],
        ]
        env_path.write_text("\n".join(rendered).strip() + "\n", encoding="utf-8")

    def _format_env_value(self, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_./:@,+-]+", value):
            return value
        return json.dumps(value)

    async def _sync_auth_store(self, agent: Agent, hermes_home: Path) -> None:
        auth_path = hermes_home / "auth.json"
        auth_store: dict = {"version": 1, "providers": {}, "credential_pool": {}}
        if auth_path.exists():
            try:
                loaded = json.loads(auth_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    auth_store.update(loaded)
            except Exception:
                pass

        credential_pool = auth_store.get("credential_pool")
        if not isinstance(credential_pool, dict):
            credential_pool = {}
            auth_store["credential_pool"] = credential_pool

        if not agent.provider:
            auth_path.write_text(json.dumps(auth_store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return

        api_key = await self._resolve_api_key(agent.api_key_ref)
        entries: list[dict] = []
        if api_key:
            base_url = (agent.base_url or "").strip()
            for priority, env_name in enumerate(self._provider_env_names(agent.provider)):
                entries.append(
                    {
                        "id": f"{env_name.lower()}-{priority}",
                        "label": env_name,
                        "auth_type": "api_key",
                        "priority": priority,
                        "source": f"env:{env_name}",
                        "access_token": api_key,
                        "last_status": None,
                        "last_status_at": None,
                        "last_error_code": None,
                        "base_url": base_url,
                        "request_count": 0,
                    }
                )
        credential_pool[agent.provider] = entries
        auth_path.write_text(json.dumps(auth_store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _provider_env_names(self, provider: str | None) -> list[str]:
        if not provider:
            return []
        try:
            from hermes_cli.auth import PROVIDER_REGISTRY

            pconfig = PROVIDER_REGISTRY.get(provider)
            envs = getattr(pconfig, "api_key_env_vars", None) if pconfig else None
            if envs:
                return list(envs)
        except Exception:
            pass
        fallback = {
            "zai": ["ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY"],
            "openrouter": ["OPENROUTER_API_KEY"],
            "anthropic": ["ANTHROPIC_API_KEY"],
            "openai": ["OPENAI_API_KEY"],
        }
        return fallback.get(provider, [])

    def _provider_base_url_env_name(self, provider: str | None) -> str | None:
        if not provider:
            return None
        try:
            from hermes_cli.auth import PROVIDER_REGISTRY

            pconfig = PROVIDER_REGISTRY.get(provider)
            base_url_env = getattr(pconfig, "base_url_env_var", None) if pconfig else None
            if isinstance(base_url_env, str) and base_url_env.strip():
                return base_url_env.strip()
        except Exception:
            pass
        fallback = {
            "zai": "GLM_BASE_URL",
            "openrouter": "OPENROUTER_BASE_URL",
            "openai": "OPENAI_BASE_URL",
            "anthropic": None,
        }
        return fallback.get(provider)
