from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from hermeshq.core.security import (
    create_device_token,
    decode_device_token_claims,
)
from hermeshq.models.agent import Agent
from hermeshq.models.enrolled_device import EnrolledDevice
from hermeshq.services.permission_enforcer import PermissionEnforcer


def hash_enroll_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


ENROLLMENT_TOKEN_TTL_MINUTES = 15
DEVICE_STALE_AFTER_MINUTES = 15
BUNDLE_VERSION = 1

VALID_FAIL_MODES = ("fail-open", "fail-closed")
FAIL_GRACE_PATTERN = "fail-grace"


class EnrollmentError(RuntimeError):
    pass


def _normalize_fail_mode(value: str | None) -> str:
    mode = (value or "fail-open").strip()
    if mode in VALID_FAIL_MODES or mode.startswith(f"{FAIL_GRACE_PATTERN}:"):
        return mode
    return "fail-open"


def parse_fail_mode(mode: str | None) -> tuple[str, int]:
    normalized = _normalize_fail_mode(mode)
    if normalized.startswith(f"{FAIL_GRACE_PATTERN}:"):
        try:
            seconds = int(normalized.split(":", 1)[1])
        except ValueError:
            return "fail-open", 0
        return "fail-grace", max(0, seconds)
    return normalized, 0


def generate_enrollment_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


class EnrollmentService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        enforcer: PermissionEnforcer,
        secret_vault=None,
    ) -> None:
        self.session_factory = session_factory
        self._enforcer = enforcer
        self._vault = secret_vault

    # ── enrollment flow ──────────────────────────────────────────────────────

    async def create_enrollment(
        self,
        agent_id: str,
        user_id: str,
        device_name: str,
        os_info: dict | None = None,
        guard_fail_mode: str = "fail-open",
    ) -> tuple[EnrolledDevice, str]:
        async with self.session_factory() as session:
            agent = await session.get(Agent, agent_id)
            if not agent or agent.is_archived:
                raise EnrollmentError("Agent not found")
            device = EnrolledDevice(
                agent_id=agent_id,
                user_id=user_id,
                name=device_name[:128] or "Unnamed device",
                os_info=os_info or {},
                status="pending",
                guard_fail_mode=_normalize_fail_mode(guard_fail_mode),
            )
            session.add(device)
            await session.commit()
            await session.refresh(device)
            token = secrets.token_urlsafe(32)
            device.enroll_token_hash = hash_enroll_token(token)
            await session.commit()
            return device, token

    async def activate_device(self, device_id: str, enroll_token: str, os_info: dict | None = None) -> EnrolledDevice:
        async with self.session_factory() as session:
            device = cast(EnrolledDevice, await session.get(EnrolledDevice, device_id))
            if not device:
                raise EnrollmentError("Device not found")
            if device.status != "pending":
                raise EnrollmentError("Device is not pending activation")
            if not device.enroll_token_hash or device.enroll_token_hash != hash_enroll_token(enroll_token):
                raise EnrollmentError("Invalid enrollment token")
            device.status = "active"
            device.enroll_token_hash = None
            if os_info:
                device.os_info = {**(device.os_info or {}), **os_info}
            device.last_heartbeat = datetime.now(UTC)
            await session.commit()
            await session.refresh(device)
            return device

    async def issue_device_token(self, device_id: str) -> str:
        async with self.session_factory() as session:
            device = cast(EnrolledDevice, await session.get(EnrolledDevice, device_id))
            if not device:
                raise EnrollmentError("Device not found")
            if device.status != "active":
                raise EnrollmentError("Device is not active")
            return create_device_token(device.agent_id, device.id, device.token_version)

    async def resolve_device_by_token(self, token: str) -> EnrolledDevice | None:
        claims = decode_device_token_claims(token)
        if not claims:
            return None
        device_id = claims.get("did")
        async with self.session_factory() as session:
            device = cast(EnrolledDevice, await session.get(EnrolledDevice, device_id))
            if not device or device.status != "active":
                return None
            if claims.get("sub") != device.agent_id or claims.get("token_version") != device.token_version:
                return None
            return device

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def revoke_device(self, device_id: str) -> EnrolledDevice:
        async with self.session_factory() as session:
            device = cast(EnrolledDevice, await session.get(EnrolledDevice, device_id))
            if not device:
                raise EnrollmentError("Device not found")
            device.status = "revoked"
            device.revoked_at = datetime.now(UTC)
            device.token_version += 1
            await session.commit()
            await session.refresh(device)
            return device

    async def heartbeat(
        self,
        device: EnrolledDevice,
        *,
        hermes_version: str | None = None,
        bundle_etag: str | None = None,
    ) -> dict:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            managed = await session.get(EnrolledDevice, device.id)
            if not managed:
                raise EnrollmentError("Device disappeared")
            managed.last_heartbeat = now
            if hermes_version:
                managed.hermes_version = hermes_version
            if bundle_etag:
                managed.last_sync_at = now
                managed.bundle_etag = bundle_etag
            await session.commit()
        return {"status": "ok", "server_time": now.isoformat()}

    async def list_devices(self, agent_id: str | None = None) -> list[EnrolledDevice]:
        async with self.session_factory() as session:
            statement = select(EnrolledDevice).order_by(EnrolledDevice.created_at.desc())
            if agent_id:
                statement = statement.where(EnrolledDevice.agent_id == agent_id)
            result = await session.execute(statement)
            return list(result.scalars().all())

    async def get_device(self, device_id: str) -> EnrolledDevice | None:
        async with self.session_factory() as session:
            device = await session.get(EnrolledDevice, device_id)
            return device if device is None else cast(EnrolledDevice, device)

    @staticmethod
    def device_stale(device: EnrolledDevice) -> bool:
        if device.status != "active" or not device.last_heartbeat:
            return False
        return datetime.now(UTC) - device.last_heartbeat > timedelta(minutes=DEVICE_STALE_AFTER_MINUTES)

    async def build_bundle(self, device: EnrolledDevice) -> dict:
        import json

        from hermeshq.config import get_settings
        from hermeshq.services.provider_catalog import normalize_runtime_provider

        async with self.session_factory() as session:
            agent = cast(Agent, await session.get(Agent, device.agent_id))
            if not agent or agent.is_archived:
                raise EnrollmentError("Agent not found")
            merged = await self._enforcer.get_policies(agent)
            policies_payload = [
                {
                    "name": p.name,
                    "tool_rules": p.tool_rules,
                    "path_rules": p.path_rules,
                    "command_rules": p.command_rules,
                    "network_rules": p.network_rules,
                }
                for p in merged
            ]
            api_key = await self._resolve_agent_api_key(agent)

        settings = get_settings()
        guard_files = self._guard_plugin_files()
        guard_api_url = settings.enrollment_public_api_url or settings.internal_api_base_url
        runtime_provider = normalize_runtime_provider(agent.provider)
        custom_provider = None
        model_provider = runtime_provider or ""
        if runtime_provider == "openai-codex" and (api_key or agent.base_url):
            model_provider = "hermeshq-openai-compatible"
            custom_provider = {
                "name": "HermesHQ OpenAI-compatible",
                "base_url": (agent.base_url or "").strip(),
                "key_env": "OPENAI_API_KEY",
                "default_model": agent.model,
            }
        payload = {
            "version": BUNDLE_VERSION,
            "agent": {
                "id": agent.id,
                "name": agent.name,
                "friendly_name": agent.friendly_name,
                "system_prompt": agent.system_prompt,
                "soul_md": agent.soul_md,
                "model": agent.model,
                "provider": agent.provider,
                "model_provider": model_provider,
                "custom_provider": custom_provider,
                "base_url": agent.base_url,
                "hermes_version": agent.hermes_version,
                "api_key": api_key,
            },
            "guard": {
                "fail_mode": device.guard_fail_mode,
                "policies": policies_payload,
                "plugin_files": guard_files,
                "env": {
                    "HERMESHQ_AGENT_ID": agent.id,
                    "HERMESHQ_AGENT_TOKEN": create_device_token(agent.id, device.id, device.token_version),
                    "HERMESHQ_INTERNAL_API_URL": guard_api_url.rstrip("/"),
                },
            },
            "skills": list(agent.skills or []),
        }
        canonical = json.dumps(payload, sort_keys=True).encode()
        return {"etag": hashlib.sha256(canonical).hexdigest()[:32], "bundle": payload}

    async def _resolve_agent_api_key(self, agent: Agent) -> str | None:
        from hermeshq.services.credentials import resolve_secret_value

        if not agent.api_key_ref or self._vault is None:
            return None
        async with self.session_factory() as session:
            return await resolve_secret_value(session, self._vault, agent.api_key_ref)

    @staticmethod
    def _guard_plugin_files() -> dict[str, str]:
        from hermeshq.services.managed_capabilities import plugin_templates_root

        plugin_root = plugin_templates_root() / "hermeshq_guard"
        files: dict[str, str] = {}
        for path in sorted(plugin_root.rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".yaml", ".yml", ".md", ".txt"}:
                continue
            try:
                files[str(path.relative_to(plugin_root))] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        return files
