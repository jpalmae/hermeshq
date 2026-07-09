import json
import logging
import os
import re
import shutil
import stat
import time
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.config import get_settings
from hermeshq.core.security import create_agent_service_token
from hermeshq.models.agent import Agent
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.provider import ProviderDefinition
from hermeshq.models.secret import Secret
from hermeshq.services.agent_hierarchy import delegate_route, route_label
from hermeshq.services.hermes_version_manager import HermesRuntimeSelection, HermesVersionManager
from hermeshq.services.managed_capabilities import (
    fetch_local_skill_bundle,
    get_managed_integration,
    list_available_integration_packages,
    list_local_skill_templates,
    list_managed_integrations,
    list_managed_plugins,
    plugin_templates_root,
)
from hermeshq.services.provider_catalog import normalize_runtime_provider
from hermeshq.services.runtime_profiles import get_runtime_profile
from hermeshq.services.secret_vault import SecretVault

logger = logging.getLogger(__name__)

# Keys from os.environ that should NEVER leak into agent subprocesses.
_SENSITIVE_ENV_PREFIXES = (
    "AWS_", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CREDENTIALS",
    "KUBECONFIG", "DOCKER_", "GITHUB_TOKEN", "GITLAB_TOKEN",
    "HEROKU_API_KEY", "STRIPE_", "TWILIO_", "SENDGRID_",
    "DATABASE_URL", "REDIS_URL", "RABBITMQ_", "KAFKA_",
    "LDAP_", "VAULT_TOKEN", "VAULT_ADDR",
    "HERMESHQ_",  # our own internal secrets
)


def _protect_file(path: Path) -> None:
    """Set file permissions to owner-only read/write (0o600) for files containing secrets."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        logger.warning("Could not set restrictive permissions on %s", path)


def _build_safe_env() -> dict[str, str]:
    """Build a sanitized copy of os.environ with sensitive keys removed."""
    safe: dict[str, str] = {}
    for key, value in os.environ.items():
        if any(key.upper().startswith(prefix) for prefix in _SENSITIVE_ENV_PREFIXES):
            continue
        safe[key] = value
    return safe


# ---------------------------------------------------------------------------
# In-memory cache for sync_agent_installation results (avoids redundant
# disk I/O + DB queries when the same agent is checked repeatedly).
# ---------------------------------------------------------------------------
_INSTALL_CACHE: dict[str, tuple[float, list[dict]]] = {}
_INSTALL_CACHE_TTL = 60  # seconds


def _get_install_cached(agent_id: str) -> list[dict] | None:
    entry = _INSTALL_CACHE.get(agent_id)
    if entry and (time.monotonic() - entry[0]) < _INSTALL_CACHE_TTL:
        return entry[1]
    return None


def _set_install_cached(agent_id: str, result: list[dict]) -> None:
    _INSTALL_CACHE[agent_id] = (time.monotonic(), result)


def _invalidate_install_cached(agent_id: str) -> None:
    _INSTALL_CACHE.pop(agent_id, None)


class HermesInstallationError(RuntimeError):
    pass


class HermesInstallationManager:
    _DESC_RE = re.compile(r"^\s*description:\s*(.+?)\s*$", re.MULTILINE)
    _WHATSAPP_BRIDGE_SOURCE = Path(__file__).resolve().parents[1] / "assets" / "whatsapp-bridge"
    _CUSTOM_OPENAI_PROVIDER_KEY = "hermeshq-openai-compatible"
    _CUSTOM_OPENAI_PROVIDER_NAME = "HermesHQ OpenAI-compatible"
    _VOICE_PRESET_DEFAULTS = {
        "es": {
            "edge_voice": "es-MX-JorgeNeural",
            "local_voice": "es_MX-voice",
        },
        "en": {
            "edge_voice": "en-US-GuyNeural",
            "local_voice": "en_US-voice",
        },
    }

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_vault: SecretVault,
        version_manager: HermesVersionManager,
    ) -> None:
        self.session_factory = session_factory
        self.secret_vault = secret_vault
        self.version_manager = version_manager

    def resolve_workspace_path(self, workspace_path: str) -> Path:
        path = Path(workspace_path)
        if path.is_absolute():
            return path
        project_root = Path(__file__).resolve().parents[2]
        return (project_root / path).resolve()

    def build_hermes_home(self, workspace_path: str) -> Path:
        return self.resolve_workspace_path(workspace_path) / ".hermes"

    async def sync_agent_installation(self, agent: Agent) -> list[dict]:
        cached = _get_install_cached(agent.id)
        if cached is not None:
            return cached

        hermes_home = self.build_hermes_home(agent.workspace_path)
        self._ensure_home_dirs(hermes_home)
        enabled_integrations = await self._load_enabled_integration_slugs()
        runtime_selection = await self.resolve_hermes_runtime(agent)
        self._sync_managed_plugins(agent, hermes_home, enabled_integrations)
        active_skin = await self._sync_global_skin(hermes_home)
        app_name = await self._get_instance_app_name()
        installed_skills = await self._sync_managed_skills(agent, hermes_home, enabled_integrations)
        system_prompt = await self._build_system_prompt(agent, installed_skills, app_name)
        messaging_channels = await self._load_messaging_channels(agent.id)
        self._sync_whatsapp_bridge_assets(hermes_home)
        # Pre-resolve auxiliary API keys for config.yaml (sync method can't do async)
        resolved_aux_api_keys = await self._resolve_auxiliary_api_keys(agent)
        # Resolve effective model: when use_provider_default=True, use the provider's
        # current default_model from DB instead of the snapshot taken at creation time.
        effective_model = await self._resolve_effective_model(agent)
        self._write_config(agent, hermes_home, system_prompt, messaging_channels, active_skin, runtime_selection, resolved_aux_api_keys, effective_model)
        self._write_soul(agent, hermes_home, app_name)
        await self._sync_auth_store(agent, hermes_home)
        await self._sync_dotenv(agent, hermes_home, messaging_channels)
        _set_install_cached(agent.id, installed_skills)
        return installed_skills

    async def build_process_env(self, agent: Agent, *, include_channels: bool = True) -> dict[str, str]:
        hermes_home = self.build_hermes_home(agent.workspace_path)
        profile = get_runtime_profile(agent.runtime_profile)
        runtime_provider = normalize_runtime_provider(agent.provider)
        effective_base_url = self._effective_provider_base_url(agent)
        env = {**_build_safe_env(), "HERMES_HOME": str(hermes_home), "TERM": "xterm-256color"}
        env["HERMESHQ_AGENT_ID"] = agent.id
        env["HERMESHQ_AGENT_TOKEN"] = create_agent_service_token(agent.id)
        env["HERMESHQ_INTERNAL_API_URL"] = get_settings().internal_api_base_url.rstrip("/")
        env["HERMESHQ_RUNTIME_PROFILE"] = profile["slug"]
        env["HERMESHQ_RUNTIME_PROFILE_NAME"] = profile["name"]
        runtime_selection = await self.resolve_hermes_runtime(agent)
        env["HERMESHQ_HERMES_RUNTIME_SOURCE"] = runtime_selection.source
        env["HERMESHQ_HERMES_VERSION"] = runtime_selection.detected_version or runtime_selection.effective_version
        if runtime_selection.release_tag:
            env["HERMESHQ_HERMES_RELEASE_TAG"] = runtime_selection.release_tag
        api_key = await self._resolve_api_key(agent.api_key_ref)
        if api_key:
            for env_name in self._provider_env_names(runtime_provider):
                env[env_name] = api_key
        if effective_base_url:
            provider_base_url_env = self._provider_base_url_env_name(runtime_provider)
            if provider_base_url_env:
                env[provider_base_url_env] = effective_base_url
            # Only set OPENAI_BASE_URL when the provider actually uses it
            # (openai, openai-codex, gemini). Providers like kimi-coding,
            # zai, openrouter have their own dedicated base_url env vars.
            if runtime_provider in ("openai", "openai-codex", "gemini"):
                env["OPENAI_BASE_URL"] = effective_base_url
                if api_key and "OPENAI_API_KEY" not in env:
                    env["OPENAI_API_KEY"] = api_key
        # Seed auxiliary env vars (vision, compression, web_extract) for ALL
        # providers with an API key + base_url — not just openai/codex/gemini.
        # This ensures the gateway's vision_analyze tool can resolve
        # credentials regardless of the main provider (e.g. nous-api,
        # openai-compatible, gemini-api, etc.).
        if api_key and effective_base_url:
            for _aux_task in ("vision", "compression", "web_extract"):
                env.setdefault(f"AUXILIARY_{_aux_task.upper()}_API_KEY", api_key)
                env.setdefault(f"AUXILIARY_{_aux_task.upper()}_BASE_URL", effective_base_url)
        managed_env = await self._build_managed_env_map(agent) if include_channels else {}
        for key, value in managed_env.items():
            env[key] = value
        # ── Auxiliary model env vars ────────────────────────────────────
        if agent.auxiliary_models:
            for task_name, aux_cfg in agent.auxiliary_models.items():
                if not isinstance(aux_cfg, dict):
                    continue
                task_upper = task_name.upper()
                aux_provider = aux_cfg.get("provider")
                aux_model = aux_cfg.get("model")
                aux_base_url = aux_cfg.get("base_url")
                aux_api_key_ref = aux_cfg.get("api_key_ref")
                if aux_provider:
                    env[f"AUXILIARY_{task_upper}_PROVIDER"] = aux_provider
                if aux_model:
                    env[f"AUXILIARY_{task_upper}_MODEL"] = aux_model
                if aux_base_url:
                    env[f"AUXILIARY_{task_upper}_BASE_URL"] = aux_base_url
                if aux_api_key_ref:
                    aux_api_key = await self._resolve_api_key(aux_api_key_ref)
                    if aux_api_key:
                        env[f"AUXILIARY_{task_upper}_API_KEY"] = aux_api_key
        return env

    async def build_gateway_env(self, agent: Agent, platform: str | None = None) -> dict[str, str]:
        env = await self.build_process_env(agent, include_channels=False)
        for key, value in (await self._build_managed_env_map(agent, platform)).items():
            env[key] = value
        # ── Inject fallback provider env vars ───────────────────────────
        # The gateway subprocess runs its own conversation loop and needs
        # the fallback credentials in its environment so that Hermes Agent
        # can retry with the secondary provider on a primary failure (429,
        # auth, timeout, etc.).
        if agent.fallback_provider or agent.fallback_api_key_ref:
            fallback_env = await self._build_fallback_gateway_env(agent)
            env.update(fallback_env)
        # WhatsApp pairing requires scanning a QR code — give enough time for the
        # user to open the app and scan before the gateway times out.
        if platform == "whatsapp" or (platform is None and env.get("WHATSAPP_ENABLED") == "true"):
            env.setdefault("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "120")
        # Resolve HERMESHQ_RESOLVED_USER_ID so M365 plugins can identify the user
        # when the native gateway (Telegram, WhatsApp) doesn't inject thread_user_id.
        if "HERMESHQ_RESOLVED_USER_ID" not in env:
            resolved = await self._resolve_gateway_user_id(agent, platform)
            if resolved:
                env["HERMESHQ_RESOLVED_USER_ID"] = resolved
        return env

    async def _build_fallback_gateway_env(self, agent: Agent) -> dict[str, str]:
        """Build env vars for the fallback provider (used by the gateway subprocess)."""
        env: dict[str, str] = {}
        fallback_provider = normalize_runtime_provider(agent.fallback_provider)
        if not fallback_provider:
            return env
        fallback_api_key = await self._resolve_api_key(agent.fallback_api_key_ref)
        if fallback_api_key:
            for env_name in self._provider_env_names(fallback_provider):
                env[env_name] = fallback_api_key
        if agent.fallback_base_url:
            base_env = self._provider_base_url_env_name(fallback_provider)
            if base_env:
                env[base_env] = agent.fallback_base_url
        # Expose fallback config via dedicated env vars so Hermes Agent or
        # future retry logic can read them.
        env["HERMESHQ_FALLBACK_MODEL"] = agent.fallback_model or ""
        env["HERMESHQ_FALLBACK_PROVIDER"] = fallback_provider
        env["HERMESHQ_FALLBACK_BASE_URL"] = agent.fallback_base_url or ""
        return env

    async def _resolve_gateway_user_id(self, agent: Agent, platform: str | None) -> str | None:
        """Return the HermesHQ user ID for a single-user WhatsApp channel, or None."""
        from hermeshq.models.user import User
        if platform not in (None, "whatsapp"):
            return None
        channels = await self._load_messaging_channels(agent.id)
        for channel in channels:
            if channel.platform != "whatsapp":
                continue
            if not self._channel_runtime_enabled(channel):
                continue
            allowed = list(channel.allowed_user_ids or [])
            if not allowed:
                continue
            async with self.session_factory() as session:
                result = await session.execute(
                    select(User).where(User.whatsapp_user.in_(allowed)).limit(2)
                )
                users = result.scalars().all()
            if len(users) == 1:
                return users[0].id
        return None

    async def get_runtime_system_prompt(self, agent: Agent) -> str:
        installed = await self.list_installed_skills(agent)
        return await self._build_system_prompt(agent, installed, await self._get_instance_app_name())

    async def list_installed_skills(self, agent: Agent) -> list[dict]:
        hermes_home = self.build_hermes_home(agent.workspace_path)
        return self._scan_installed_skills(hermes_home)

    async def delete_installed_skill(self, agent: Agent, installed_path: str) -> list[dict]:
        hermes_home = self.build_hermes_home(agent.workspace_path)
        skills_root = (hermes_home / "skills").resolve()
        target_dir = (skills_root / installed_path).resolve()

        try:
            target_dir.relative_to(skills_root)
        except ValueError as exc:
            raise HermesInstallationError("Invalid skill path") from exc

        if not target_dir.exists() or not target_dir.is_dir() or not (target_dir / "SKILL.md").exists():
            raise HermesInstallationError("Installed skill not found")

        metadata_path = target_dir / ".hermeshq-skill.json"
        metadata: dict | None = None
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                metadata = None

        managed_identifier = metadata.get("identifier") if isinstance(metadata, dict) else None
        is_managed = "hermeshq-managed" in target_dir.parts

        if is_managed:
            current_skills = [skill for skill in agent.skills if isinstance(skill, str) and skill.strip()]
            if managed_identifier:
                agent.skills = [skill for skill in current_skills if skill != managed_identifier]
            else:
                expected_name = target_dir.name
                agent.skills = [
                    skill
                    for skill in current_skills
                    if skill.strip().split("/")[-1] != expected_name
                ]

        shutil.rmtree(target_dir)
        _invalidate_install_cached(agent.id)
        return await self.sync_agent_installation(agent)

    async def search_catalog(self, query: str, limit: int = 20) -> list[dict]:
        from tools.skills_hub import GitHubAuth, OptionalSkillSource, SkillsShSource

        results: list[dict] = []
        seen: set[str] = set()
        enabled_integrations = await self._load_enabled_integration_slugs()
        for meta in list_local_skill_templates(query, limit=limit, enabled_integration_slugs=enabled_integrations):
            if meta["identifier"] in seen:
                continue
            seen.add(meta["identifier"])
            results.append(meta)
            if len(results) >= limit:
                return results
        sources = [OptionalSkillSource(), SkillsShSource(GitHubAuth())]

        for source in sources:
            try:
                found = source.search(query, limit=limit)
            except Exception:  # noqa: BLE001  # skill search best-effort
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

    def _channel_runtime_enabled(self, channel: MessagingChannel) -> bool:
        metadata = channel.metadata_json if isinstance(channel.metadata_json, dict) else {}
        return bool(channel.enabled) and not bool(metadata.get("runtime_disabled"))

    def _sync_managed_plugins(self, agent: Agent, hermes_home: Path, enabled_integration_slugs: list[str]) -> None:
        plugins_root = hermes_home / "plugins"
        plugins_root.mkdir(parents=True, exist_ok=True)
        desired_plugins = list_managed_plugins(
            enabled_integration_slugs,
            include_system_plugins=bool(agent.is_system_agent),
        )
        desired_names = {plugin["template_dir"] for plugin in desired_plugins}
        known_names = {
            plugin["template_dir"]
            for plugin in list_managed_plugins([], include_system_plugins=True)
        }
        known_names.update(
            package["plugin_slug"]
            for package in list_available_integration_packages([])
            if package.get("plugin_slug")
        )
        for existing in plugins_root.iterdir():
            if existing.is_dir() and existing.name in known_names and existing.name not in desired_names:
                shutil.rmtree(existing)
        templates_root = plugin_templates_root()
        for plugin in desired_plugins:
            source_root = Path(plugin["source_root"]) if plugin.get("source_root") else templates_root / plugin["template_dir"]
            target_root = plugins_root / plugin["template_dir"]
            if target_root.exists():
                shutil.rmtree(target_root)
            shutil.copytree(source_root, target_root)

    async def _sync_global_skin(self, hermes_home: Path) -> str | None:
        skins_root = hermes_home / "skins"
        skins_root.mkdir(parents=True, exist_ok=True)
        for path in list(skins_root.glob("hermeshq-global-*.y*ml")):
            path.unlink()

        async with self.session_factory() as session:
            app_settings = await session.get(AppSettings, "default")
            if not app_settings or not app_settings.default_tui_skin or not app_settings.tui_skin_filename:
                return None
            source_path = get_settings().hermes_skins_root / app_settings.tui_skin_filename
            if not source_path.exists():
                return None
            target_path = skins_root / app_settings.tui_skin_filename
            shutil.copy2(source_path, target_path)
            return Path(app_settings.tui_skin_filename).stem

    def _write_config(
        self,
        agent: Agent,
        hermes_home: Path,
        system_prompt: str,
        messaging_channels: list[MessagingChannel],
        active_skin: str | None,
        runtime_selection: HermesRuntimeSelection,
        resolved_aux_api_keys: dict[str, str] | None = None,
        effective_model: str | None = None,
    ) -> None:
        profile = get_runtime_profile(agent.runtime_profile)
        telegram_channel = next((item for item in messaging_channels if item.platform == "telegram"), None)
        whatsapp_channel = next((item for item in messaging_channels if item.platform == "whatsapp"), None)
        teams_channel = next((item for item in messaging_channels if item.platform == "microsoft_teams"), None)
        sixagentic_channel = next((item for item in messaging_channels if item.platform == "sixagentic"), None)
        model_provider = self._model_provider_for_agent(agent)
        effective_base_url = self._effective_provider_base_url(agent)
        config = {
            "model": {
                "default": effective_model or agent.model,
                "provider": model_provider,
                "base_url": effective_base_url,
            },
            "agent": {
                "max_turns": agent.max_iterations,
                "system_prompt": system_prompt,
            },
            "runtime_profile": {
                "slug": profile["slug"],
                "name": profile["name"],
                "tooling_summary": profile["tooling_summary"],
            },
            "hermes_runtime": {
                "source": runtime_selection.source,
                "version": runtime_selection.detected_version or runtime_selection.effective_version,
                "release_tag": runtime_selection.release_tag or "",
            },
            "skills": {
                "external_dirs": [],
            },
        }
        if self._uses_custom_openai_provider(agent):
            config["providers"] = {
                self._CUSTOM_OPENAI_PROVIDER_KEY: {
                    "name": self._CUSTOM_OPENAI_PROVIDER_NAME,
                    "base_url": effective_base_url,
                    "key_env": "OPENAI_API_KEY",
                    "default_model": effective_model or agent.model,
                }
            }
        if active_skin:
            config["display"] = {"skin": active_skin}
        if telegram_channel and self._channel_runtime_enabled(telegram_channel):
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
        if whatsapp_channel and self._channel_runtime_enabled(whatsapp_channel):
            platforms = config.setdefault("platforms", {})
            whatsapp_platform = {
                "enabled": bool(whatsapp_channel.enabled),
                "extra": {
                    "bridge_script": str(self._whatsapp_bridge_target(hermes_home) / "bridge.js"),
                    "bridge_port": self._whatsapp_bridge_port(agent),
                    "session_path": str(self._whatsapp_session_dir(hermes_home)),
                    "require_mention": bool(whatsapp_channel.require_mention),
                    "free_response_chats": list(whatsapp_channel.free_response_chat_ids or []),
                },
            }
            if whatsapp_channel.home_chat_id:
                whatsapp_platform["home_channel"] = {
                    "platform": "whatsapp",
                    "chat_id": whatsapp_channel.home_chat_id,
                    "name": whatsapp_channel.home_chat_name or "Home",
                }
            platforms["whatsapp"] = whatsapp_platform
        if teams_channel and self._channel_runtime_enabled(teams_channel):
            platforms = config.setdefault("platforms", {})
            teams_platform = {
                "enabled": bool(teams_channel.enabled),
                "extra": {},
            }
            if teams_channel.home_chat_id:
                teams_platform["home_channel"] = {
                    "platform": "teams",
                    "chat_id": teams_channel.home_chat_id,
                    "name": teams_channel.home_chat_name or "Home",
                }
            platforms["teams"] = teams_platform
        if sixagentic_channel and self._channel_runtime_enabled(sixagentic_channel):
            platforms = config.setdefault("platforms", {})
            platforms["sixagentic"] = {
                "enabled": bool(sixagentic_channel.enabled),
            }
        voice_overrides = self._voice_runtime_overrides(agent)
        if voice_overrides:
            config.update(voice_overrides)
        interaction_overrides = self._interaction_runtime_overrides(agent)
        if interaction_overrides:
            for section, values in interaction_overrides.items():
                existing = config.setdefault(section, {})
                if isinstance(existing, dict) and isinstance(values, dict):
                    existing.update(values)
                else:
                    config[section] = values
        # ── Auxiliary models ────────────────────────────────────────────
        aux_section = {}
        if agent.auxiliary_models:
            for task_name, aux_cfg in agent.auxiliary_models.items():
                if not isinstance(aux_cfg, dict):
                    continue
                entry = {}
                provider_val = aux_cfg.get("provider")
                base_url_val = aux_cfg.get("base_url")
                # When a custom base_url is set, Hermes Agent expects provider="custom"
                if base_url_val:
                    entry["provider"] = "custom"
                    entry["base_url"] = base_url_val
                elif provider_val:
                    entry["provider"] = provider_val
                if aux_cfg.get("model"):
                    entry["model"] = aux_cfg["model"]
                if aux_cfg.get("api_key"):
                    entry["api_key"] = aux_cfg["api_key"]
                elif resolved_aux_api_keys and task_name in resolved_aux_api_keys:
                    entry["api_key"] = resolved_aux_api_keys[task_name]
                if entry:
                    aux_section[task_name] = entry
        # Auto-seed auxiliary tasks (vision, compression, web_extract) from
        # the agent's main provider when there is an explicit API key and
        # base URL.  Previously limited to openai/codex/gemini, this now
        # covers ALL providers so that gateway tools (e.g. vision_analyze)
        # can resolve credentials from config.yaml regardless of the main
        # provider (nous-api, openai-compatible, gemini-api, etc.).
        if (
            effective_base_url
            and resolved_aux_api_keys is not None
            and resolved_aux_api_keys.get("__main__")
        ):
            main_key = resolved_aux_api_keys["__main__"]
            for task_name in ("vision", "compression", "web_extract"):
                if task_name not in aux_section:
                    aux_section[task_name] = {
                        "provider": "custom",
                        "base_url": effective_base_url,
                        "api_key": main_key,
                    }
        if aux_section:
            config["auxiliary"] = aux_section
        # ── Plugins: enable plugins installed in HERMES_HOME/plugins/ ───────
        # Hermes requires an explicit plugins.enabled list in config.yaml to
        # load plugins from the plugins/ directory; without it, plugins are
        # discovered but skipped. We include every plugin slug in enabled_toolsets
        # that has a corresponding directory in HERMES_HOME/plugins/.
        plugins_root = hermes_home / "plugins"
        if plugins_root.exists():
            installed_plugin_dirs = {p.name for p in plugins_root.iterdir() if p.is_dir()}
            # enabled_toolsets contains slugs like "hermeshq_ms365_mail"
            plugins_to_enable = [
                slug for slug in (agent.enabled_toolsets or [])
                if slug in installed_plugin_dirs
            ]
            if plugins_to_enable:
                config["plugins"] = {"enabled": plugins_to_enable}
        config_path = hermes_home / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        # config.yaml may contain API keys — protect with owner-only permissions
        _protect_file(config_path)

    async def _resolve_effective_model(self, agent: Agent) -> str | None:
        """Resolve the effective model for the agent.

        When use_provider_default=True, look up the provider's current
        default_model from the DB so that changes to provider.default_model
        propagate to existing agents.  When use_provider_default=False or the
        provider has no default_model, fall back to agent.model.
        """
        if not agent.use_provider_default:
            return agent.model
        async with self.session_factory() as session:
            provider = await session.get(ProviderDefinition, agent.provider)
            if provider and provider.default_model:
                return provider.default_model
        return agent.model

    async def _resolve_auxiliary_api_keys(self, agent: Agent) -> dict[str, str]:
        """Pre-resolve auxiliary API keys so _write_config can include them in config.yaml."""
        result: dict[str, str] = {}
        # Always resolve the main provider API key so _write_config can
        # auto-seed auxiliary tasks (vision, compression, web_extract) for
        # OpenAI-compatible providers that don't go through OAuth.
        main_key = await self._resolve_api_key(agent.api_key_ref)
        if main_key:
            result["__main__"] = main_key
        if agent.auxiliary_models:
            for task_name, aux_cfg in agent.auxiliary_models.items():
                if not isinstance(aux_cfg, dict):
                    continue
                ref = aux_cfg.get("api_key_ref")
                if ref:
                    key = await self._resolve_api_key(ref)
                    if key:
                        result[task_name] = key
        return result

    def _interaction_runtime_overrides(self, agent: Agent) -> dict[str, dict]:
        overrides: dict[str, dict] = {}
        approval_mode = (agent.approval_mode or "").strip()
        if approval_mode:
            overrides["approvals"] = {"mode": approval_mode}
        tool_progress_mode = (agent.tool_progress_mode or "").strip()
        if tool_progress_mode:
            overrides["display"] = {"tool_progress": tool_progress_mode}
        gateway_notifications_mode = (agent.gateway_notifications_mode or "").strip()
        if gateway_notifications_mode:
            overrides["gateway"] = {"notifications": gateway_notifications_mode}
        return overrides

    def _whatsapp_bridge_target(self, hermes_home: Path) -> Path:
        return hermes_home / "platforms" / "whatsapp-bridge"

    def _whatsapp_session_dir(self, hermes_home: Path) -> Path:
        return hermes_home / "whatsapp" / "session"

    def _whatsapp_bridge_port(self, agent: Agent) -> int:
        raw = (agent.id or "").replace("-", "")
        seed = int(raw[:8] or "0", 16)
        return 32000 + (seed % 2000)

    def _sync_whatsapp_bridge_assets(self, hermes_home: Path) -> None:
        source_root = self._WHATSAPP_BRIDGE_SOURCE
        if not source_root.exists():
            return
        target_root = self._whatsapp_bridge_target(hermes_home)
        target_root.mkdir(parents=True, exist_ok=True)
        for source_path in source_root.iterdir():
            target_path = target_root / source_path.name
            if source_path.is_dir():
                shutil.copytree(source_path, target_path, dirs_exist_ok=True)
                continue
            shutil.copy2(source_path, target_path)

    async def resolve_hermes_runtime(self, agent: Agent) -> HermesRuntimeSelection:
        async with self.session_factory() as session:
            app_settings = await session.get(AppSettings, "default")
            requested_version = agent.hermes_version or (app_settings.default_hermes_version if app_settings else None)
        return await self.version_manager.resolve_runtime(requested_version)

    def _write_soul(self, agent: Agent, hermes_home: Path, app_name: str) -> None:
        (hermes_home / "SOUL.md").write_text(
            agent.soul_md or f"# Soul\n\n{app_name} managed agent.",
            encoding="utf-8",
        )

    async def _sync_managed_skills(self, agent: Agent, hermes_home: Path, enabled_integration_slugs: list[str]) -> list[dict]:
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

            try:
                bundle = await self._fetch_skill_bundle(identifier, enabled_integration_slugs)
            except HermesInstallationError as exc:
                logger.warning("Skipping skill '%s': %s", identifier, exc)
                continue
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

    async def _fetch_skill_bundle(self, identifier: str, enabled_integration_slugs: list[str]) -> dict:
        from tools.skills_hub import GitHubAuth, OptionalSkillSource, SkillsShSource, WellKnownSkillSource

        identifier = identifier.strip()
        if identifier.startswith("local/"):
            bundle = fetch_local_skill_bundle(identifier, enabled_integration_slugs=enabled_integration_slugs)
            if not bundle:
                raise HermesInstallationError(f"Local skill '{identifier}' was not found")
            return bundle
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
            except OSError:
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
                except (json.JSONDecodeError, OSError):
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
        text = skill_md.decode("utf-8", errors="replace") if isinstance(skill_md, bytes) else skill_md
        match = self._DESC_RE.search(text)
        if match:
            return match.group(1).strip().strip("\"'")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("---") and not stripped.startswith("#"):
                return stripped[:200]
        return ""

    async def _build_system_prompt(self, agent: Agent, installed_skills: list[dict], app_name: str) -> str:
        roster = await self._load_agent_roster(agent)
        enabled_integrations = await self._load_enabled_integration_slugs()
        return self._compose_system_prompt(agent, installed_skills, roster, app_name, enabled_integrations)

    async def _load_agent_roster(self, agent: Agent) -> list[dict]:
        async with self.session_factory() as session:
            result = await session.execute(select(Agent).order_by(Agent.created_at.asc()))
            agents = list(result.scalars().all())

        name_by_id = {
            item.id: (item.friendly_name or item.name or item.slug or item.id)
            for item in agents
        }
        roster: list[dict] = []
        agent_map = {item.id: item for item in agents}
        for item in agents:
            allowed, route = delegate_route(agent_map, agent, item)
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
                    "can_send_tasks": bool(item.can_send_tasks),
                    "supervisor": name_by_id.get(item.supervisor_agent_id) if item.supervisor_agent_id else None,
                    "team_tags": list(item.team_tags or []),
                    "delegate_allowed": bool(allowed),
                    "delegate_route": route,
                }
            )
        return roster

    def _compose_system_prompt(
        self,
        agent: Agent,
        installed_skills: list[dict],
        roster: list[dict],
        app_name: str,
        enabled_integration_slugs: list[str] | None = None,
    ) -> str:
        parts = [agent.system_prompt.strip()] if agent.system_prompt and agent.system_prompt.strip() else []
        profile = get_runtime_profile(agent.runtime_profile)
        parts.append(
            "\n".join(
                [
                    f"Your current HermesHQ runtime profile is {profile['name']} ({profile['slug']}).",
                    profile["description"],
                    f"Profile intent: {profile['container_intent']}",
                    f"Runtime note: {profile['tooling_summary']}",
                    (
                        "Stage 1 note: profiles still run inside the shared HermesHQ backend image. "
                        "Treat this profile as the intended operating environment and runtime policy."
                    ),
                ]
            )
        )
        if agent.is_system_agent:
            parts.append(
                "\n".join(
                    [
                        "You are a HermesHQ system agent operating on the control plane.",
                        f"System scope: {agent.system_scope or 'operator'}.",
                        (
                            "Use typed HermesHQ control tools for administrative changes first. "
                            "Use shell access only when the typed tools do not cover the task or when direct host inspection is explicitly needed."
                        ),
                        (
                            "Administrative actions must remain explicit, auditable, and minimally destructive. "
                            "Do not disable or archive yourself."
                        ),
                    ]
                )
            )
        if installed_skills:
            lines = [
                f"{app_name} assigned skills are installed in your Hermes home.",
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
        enabled_integrations = self._describe_enabled_integrations(agent, enabled_integration_slugs or [])
        if enabled_integrations:
            lines = [
                f"{app_name} enabled managed integrations for this agent:",
            ]
            lines.extend(f"- {item}" for item in enabled_integrations)
            lines.append(
                "Do not deny these capabilities when they are listed here. If a capability depends on a specific input channel "
                "(for example audio arriving through a supported runtime/channel instead of the plain web chat box), explain that constraint explicitly."
            )
            parts.append("\n".join(lines))
        if roster:
            lines = [
                f"{app_name} live roster for this instance. This is factual control-plane data, not memory.",
                "If asked whether you know another agent, answer from this roster.",
                (
                    "You have real control-plane tools available for inter-agent communication: "
                    "`hq_list_agents`, `hq_direct_message`, and `hq_delegate_task`."
                ),
                (
                    "Do not claim that you cannot contact other agents when these tools are available. "
                    "Use `hq_direct_message` for a non-task message and `hq_delegate_task` for executable work."
                ),
                (
                    "Hierarchy is enforced by the platform: independent agents may delegate freely; "
                    "subordinates may escalate upward or delegate downward within their own branch."
                ),
                "Known agents:",
            ]
            for item in roster:
                role = item["description"] or "No description"
                supervisor = item["supervisor"] or "none"
                tag_text = ", ".join(item["team_tags"]) if item["team_tags"] else "none"
                lines.append(
                    f"- {item['display_name']} | slug={item['slug'] or 'unset'} | status={item['status']} | "
                    f"receives_tasks={'yes' if item['can_receive_tasks'] else 'no'} | "
                    f"sends_tasks={'yes' if item['can_send_tasks'] else 'no'} | supervisor={supervisor} | "
                    f"tags={tag_text} | role={role} | delegate={route_label(item['delegate_route'])}"
                )
            parts.append("\n".join(lines))
        return "\n\n".join(part for part in parts if part).strip()

    def _describe_enabled_integrations(self, agent: Agent, enabled_integration_slugs: list[str]) -> list[str]:
        descriptions: list[str] = []
        for slug, raw_config in (agent.integration_configs or {}).items():
            integration = get_managed_integration(str(slug), enabled_integration_slugs, include_uninstalled=True)
            if not integration:
                continue
            config = raw_config if isinstance(raw_config, dict) else {}
            if slug == "voice-edge":
                descriptions.append(self._voice_integration_prompt_summary("edge-tts", config))
                continue
            if slug == "voice-local":
                descriptions.append(self._voice_integration_prompt_summary("Piper local TTS", config))
                continue
            descriptions.append(
                f"{integration.get('name') or slug}: {integration.get('description') or 'Managed integration enabled.'}"
            )
        return descriptions

    def _voice_integration_prompt_summary(self, tts_backend: str, config: dict) -> str:
        stt_enabled = self._truthy_config_value(config.get("stt_enabled"), default=True)
        tts_enabled = self._truthy_config_value(config.get("tts_enabled"), default=True)
        stt_model = str(config.get("stt_model") or "small").strip() or "small"
        stt_language = str(config.get("stt_language") or "es").strip().lower() or "es"
        voice_locale = str(config.get("voice_locale") or stt_language or "es").strip().lower() or "es"
        tts_voice = str(config.get("tts_voice") or "").strip() or (
            self._VOICE_PRESET_DEFAULTS.get(voice_locale, self._VOICE_PRESET_DEFAULTS["es"])["local_voice"]
            if "piper" in tts_backend.lower()
            else self._VOICE_PRESET_DEFAULTS.get(voice_locale, self._VOICE_PRESET_DEFAULTS["es"])["edge_voice"]
        )
        return (
            f"Voice runtime enabled: speech-to-text={'yes' if stt_enabled else 'no'} "
            f"(model={stt_model}, language={stt_language}), text-to-speech={'yes' if tts_enabled else 'no'} "
            f"(backend={tts_backend}, voice={tts_voice}). This applies to supported Hermes runtime flows and audio-capable channels; "
            "the HermesHQ web chat itself remains text-first unless a dedicated audio input/output control is added."
        )

    async def _get_instance_app_name(self) -> str:
        async with self.session_factory() as session:
            settings = await session.get(AppSettings, "default")
        configured = (settings.app_name or "").strip() if settings else ""
        return configured or "HermesHQ"

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

    async def _load_enabled_integration_slugs(self) -> list[str]:
        async with self.session_factory() as session:
            app_settings = await session.get(AppSettings, "default")
            enabled = getattr(app_settings, "enabled_integration_packages", []) if app_settings else []
        return [slug for slug in enabled if isinstance(slug, str) and slug.strip()]

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
        runtime_provider = normalize_runtime_provider(agent.provider)
        effective_base_url = self._effective_provider_base_url(agent)

        api_key = await self._resolve_api_key(agent.api_key_ref)
        if api_key:
            for env_name in self._provider_env_names(runtime_provider):
                managed[env_name] = api_key
        if effective_base_url:
            provider_base_url_env = self._provider_base_url_env_name(runtime_provider)
            if provider_base_url_env:
                managed[provider_base_url_env] = effective_base_url
            if runtime_provider in ("openai", "openai-codex", "gemini"):
                managed["OPENAI_BASE_URL"] = effective_base_url
        # Seed auxiliary env vars for all providers with credentials,
        # matching the same logic in build_process_env.
        if api_key and effective_base_url:
            for _aux_task in ("vision", "compression", "web_extract"):
                managed.setdefault(f"AUXILIARY_{_aux_task.upper()}_API_KEY", api_key)
                managed.setdefault(f"AUXILIARY_{_aux_task.upper()}_BASE_URL", effective_base_url)

        channels = await self._load_messaging_channels(agent.id)
        managed["WHATSAPP_ENABLED"] = "false"
        for channel in channels:
            if platform and channel.platform != platform:
                continue
            if not self._channel_runtime_enabled(channel):
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
                continue
            if channel.platform == "whatsapp":
                whatsapp_mode = str((channel.metadata_json or {}).get("whatsapp_mode") or "self-chat").strip() or "self-chat"
                managed["WHATSAPP_ENABLED"] = "true" if channel.enabled else "false"
                managed["WHATSAPP_MODE"] = whatsapp_mode
                if channel.allowed_user_ids:
                    managed["WHATSAPP_ALLOWED_USERS"] = ",".join(channel.allowed_user_ids)
                if channel.require_mention:
                    managed["WHATSAPP_REQUIRE_MENTION"] = "true"
                if channel.free_response_chat_ids:
                    managed["WHATSAPP_FREE_RESPONSE_CHATS"] = ",".join(channel.free_response_chat_ids)
            if channel.platform == "microsoft_teams":
                teams_secret = await self._resolve_api_key(channel.secret_ref)
                if teams_secret:
                    managed["TEAMS_CLIENT_SECRET"] = teams_secret
                metadata = channel.metadata_json or {}
                if metadata.get("app_id"):
                    managed["TEAMS_CLIENT_ID"] = str(metadata["app_id"])
                if metadata.get("tenant_id"):
                    managed["TEAMS_TENANT_ID"] = str(metadata["tenant_id"])
                if channel.allowed_user_ids:
                    managed["TEAMS_ALLOWED_USERS"] = ",".join(channel.allowed_user_ids)
                if channel.home_chat_id:
                    managed["TEAMS_HOME_CHANNEL"] = channel.home_chat_id
                if channel.home_chat_name:
                    managed["TEAMS_HOME_CHANNEL_NAME"] = channel.home_chat_name
                managed["TEAMS_REQUIRE_MENTION"] = "true" if channel.require_mention else "false"
                continue

        enabled_integrations = await self._load_enabled_integration_slugs()
        for slug, config in (agent.integration_configs or {}).items():
            integration = get_managed_integration(str(slug), enabled_integrations)
            if not integration:
                continue
            config_dict = config if isinstance(config, dict) else {}
            env_map = integration.get("env_map") or {}

            base_url = str(config_dict.get("base_url") or "").strip()
            if base_url and env_map.get("base_url"):
                managed[env_map["base_url"]] = base_url

            secret_ref = str(config_dict.get("api_key_ref") or "").strip()
            if secret_ref and env_map.get("api_key_ref"):
                secret_value = await self._resolve_api_key(secret_ref)
                if secret_value:
                    managed[env_map["api_key_ref"]] = secret_value
        return managed

    def _merge_env_file(self, env_path: Path, managed_env: dict[str, str]) -> None:
        # Keys managed by the integration system — these get stripped and rewritten
        managed_keys: set[str] = {
            "OPENAI_BASE_URL",
            "HERMESHQ_SHAREPOINT_SITE_URL",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_USERS",
            "TELEGRAM_HOME_CHANNEL",
            "TELEGRAM_HOME_CHANNEL_NAME",
            "TELEGRAM_FREE_RESPONSE_CHATS",
            "TELEGRAM_REQUIRE_MENTION",
            "WHATSAPP_ENABLED",
            "WHATSAPP_MODE",
            "WHATSAPP_ALLOWED_USERS",
            "WHATSAPP_REQUIRE_MENTION",
            "WHATSAPP_FREE_RESPONSE_CHATS",
            "TEAMS_CLIENT_ID",
            "TEAMS_CLIENT_SECRET",
            "TEAMS_TENANT_ID",
            "TEAMS_ALLOWED_USERS",
            "TEAMS_HOME_CHANNEL",
            "TEAMS_HOME_CHANNEL_NAME",
            "TEAMS_REQUIRE_MENTION",
        }
        # Add all known provider API key env vars
        for provider in (
            "zai", "openrouter", "anthropic", "openai",
            "openai-codex", "kimi-coding", "gemini", "bedrock",
        ):
            managed_keys.update(self._provider_env_names(provider))
        # Add all known provider base URL env vars
        for provider in (
            "zai", "openrouter", "openai",
            "openai-codex", "kimi-coding", "gemini", "bedrock",
        ):
            base_env = self._provider_base_url_env_name(provider)
            if base_env:
                managed_keys.add(base_env)
        for integration in list_managed_integrations():
            env_map = integration.get("env_map") or {}
            managed_keys.update(value for value in env_map.values() if value)

        # Also clean up any keys that appear duplicated in the existing file
        # (these are residue from the previous append-only bug)
        if env_path.exists():
            existing_lines = env_path.read_text(encoding="utf-8").splitlines()
            key_counts: dict[str, int] = {}
            for raw_line in existing_lines:
                stripped = raw_line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    key = stripped.split("=", 1)[0].strip()
                    key_counts[key] = key_counts.get(key, 0) + 1
            for key, count in key_counts.items():
                if count > 1:
                    managed_keys.add(key)

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
        _protect_file(env_path)

    def _format_env_value(self, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_./:@,+-]+", value):
            return value
        return json.dumps(value)

    def _truthy_config_value(self, value: object, *, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _voice_runtime_overrides(self, agent: Agent) -> dict[str, dict]:
        voice_config = self._resolve_voice_integration_config(agent)
        if not voice_config:
            return {}
        locale = str(voice_config.get("voice_locale") or "es").strip().lower()
        if locale not in self._VOICE_PRESET_DEFAULTS:
            locale = "es"
        stt_enabled = self._truthy_config_value(voice_config.get("stt_enabled"), default=True)
        tts_enabled = self._truthy_config_value(voice_config.get("tts_enabled"), default=True)
        stt_model = str(voice_config.get("stt_model") or "small").strip() or "small"
        stt_language = str(voice_config.get("stt_language") or locale).strip().lower() or locale
        if stt_language not in {"auto", "es", "en"}:
            stt_language = locale
        tts_voice = str(voice_config.get("tts_voice") or "").strip()
        if not tts_voice:
            package_slug = str(voice_config.get("__integration_slug") or "")
            if package_slug == "voice-local":
                tts_voice = self._VOICE_PRESET_DEFAULTS[locale]["local_voice"]
            else:
                tts_voice = self._VOICE_PRESET_DEFAULTS[locale]["edge_voice"]
        return {
            "stt": {
                "enabled": stt_enabled,
                "model": stt_model,
                "language": stt_language,
            },
            "tts": {
                "enabled": tts_enabled,
                "voice": tts_voice,
            },
        }

    def _resolve_voice_integration_config(self, agent: Agent) -> dict[str, object] | None:
        configs = agent.integration_configs or {}
        for slug in ("voice-local", "voice-edge"):
            config = configs.get(slug)
            if isinstance(config, dict):
                return {"__integration_slug": slug, **config}
        return None

    async def _sync_auth_store(self, agent: Agent, hermes_home: Path) -> None:
        auth_path = hermes_home / "auth.json"
        auth_store: dict = {"version": 1, "providers": {}, "credential_pool": {}}
        if auth_path.exists():
            try:
                loaded = json.loads(auth_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    auth_store.update(loaded)
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to load auth store from %s", auth_path, exc_info=True)

        credential_pool = auth_store.get("credential_pool")
        if not isinstance(credential_pool, dict):
            credential_pool = {}
            auth_store["credential_pool"] = credential_pool

        runtime_provider = normalize_runtime_provider(agent.provider)
        if not runtime_provider:
            auth_path.write_text(json.dumps(auth_store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _protect_file(auth_path)
            return

        api_key = await self._resolve_api_key(agent.api_key_ref)
        entries: list[dict] = []
        if api_key:
            base_url = self._effective_provider_base_url(agent)
            for priority, env_name in enumerate(self._provider_env_names(runtime_provider)):
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
        if self._uses_custom_openai_provider(agent):
            credential_pool.pop("openai", None)
            credential_pool.pop("openai-codex", None)
            credential_pool[self._custom_openai_pool_key()] = entries
        else:
            credential_pool[runtime_provider] = entries
        auth_path.write_text(json.dumps(auth_store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _protect_file(auth_path)

    def _uses_custom_openai_provider(self, agent: Agent) -> bool:
        runtime_provider = normalize_runtime_provider(agent.provider)
        return runtime_provider == "openai-codex" and bool(agent.api_key_ref or agent.base_url)

    def _model_provider_for_agent(self, agent: Agent) -> str:
        if self._uses_custom_openai_provider(agent):
            return self._CUSTOM_OPENAI_PROVIDER_KEY
        return normalize_runtime_provider(agent.provider) or ""

    def _effective_provider_base_url(self, agent: Agent) -> str:
        base_url = (agent.base_url or "").strip()
        if self._uses_custom_openai_provider(agent):
            return self._normalize_openai_compatible_base_url(base_url)
        return base_url

    def _normalize_openai_compatible_base_url(self, base_url: str) -> str:
        cleaned = (base_url or "").strip().rstrip("/")
        if not cleaned:
            return "https://api.openai.com/v1"
        for suffix in ("/chat/completions", "/responses", "/completions"):
            if cleaned.lower().endswith(suffix):
                return cleaned[: -len(suffix)].rstrip("/")
        return cleaned

    def _custom_openai_pool_key(self) -> str:
        return f"custom:{self._CUSTOM_OPENAI_PROVIDER_NAME.lower().replace(' ', '-')}"

    def _provider_env_names(self, provider: str | None) -> list[str]:
        if not provider:
            return []
        try:
            from hermes_cli.auth import PROVIDER_REGISTRY

            pconfig = PROVIDER_REGISTRY.get(provider)
            envs = getattr(pconfig, "api_key_env_vars", None) if pconfig else None
            if envs:
                return list(envs)
        except (ImportError, KeyError, AttributeError):
            logger.debug("Provider registry env lookup failed for '%s'; using fallback", provider, exc_info=True)
        fallback = {
            "bedrock": [],
            "nous": ["NOUS_API_KEY"],
            "nous-api": ["OPENAI_API_KEY"],
            "zai": ["ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY"],
            "openrouter": ["OPENROUTER_API_KEY"],
            "anthropic": ["ANTHROPIC_API_KEY"],
            "openai": ["OPENAI_API_KEY"],
            "openai-codex": ["OPENAI_API_KEY"],
            "openai-api": ["OPENAI_API_KEY"],
            "openai-compatible": ["OPENAI_API_KEY"],
            "kimi-coding": ["KIMI_API_KEY"],
            "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            "gemini-api": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
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
        except (ImportError, KeyError, AttributeError):
            logger.debug("Provider base_url_env lookup failed for '%s'; using fallback", provider, exc_info=True)
        fallback = {
            "bedrock": "BEDROCK_BASE_URL",
            "nous": "NOUS_BASE_URL",
            "zai": "GLM_BASE_URL",
            "openrouter": "OPENROUTER_BASE_URL",
            "openai": "OPENAI_BASE_URL",
            "openai-codex": "OPENAI_BASE_URL",
            "kimi-coding": "KIMI_BASE_URL",
            "gemini": "OPENAI_BASE_URL",
            "anthropic": None,
        }
        return fallback.get(provider)
