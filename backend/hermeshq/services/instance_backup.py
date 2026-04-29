from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.config import get_settings
from hermeshq.models.activity import ActivityLog
from hermeshq.models.agent import Agent
from hermeshq.models.agent_assignment import AgentAssignment
from hermeshq.models.app_settings import AppSettings
from hermeshq.models.conversation_thread import ConversationThread
from hermeshq.models.hermes_version import HermesVersion
from hermeshq.models.integration_draft import IntegrationDraft
from hermeshq.models.message import AgentMessage
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.node import Node
from hermeshq.models.provider import ProviderDefinition
from hermeshq.models.scheduled_task import ScheduledTask
from hermeshq.models.secret import Secret
from hermeshq.models.task import Task
from hermeshq.models.template import AgentTemplate
from hermeshq.models.terminal_session import TerminalSession
from hermeshq.models.user import User
from hermeshq.schemas.backup import (
    InstanceBackupCreateRequest,
    InstanceBackupRestoreRead,
    InstanceBackupSummary,
    InstanceBackupValidationRead,
)
from hermeshq.versioning import get_app_version

BACKUP_SCHEMA_VERSION = "2026.04.28.1"
ENCRYPTED_SECRETS_PATH = "secrets/secrets.enc.json"


class InstanceBackupError(RuntimeError):
    pass


@dataclass
class RestorePayload:
    summary: InstanceBackupSummary
    database: dict[str, list[dict[str, Any]]]
    secret_rows: list[dict[str, Any]]
    extracted_root: Path
    workspace_map: dict[str, dict[str, str]]


class InstanceBackupService:
    CORE_TABLES: tuple[tuple[str, type], ...] = (
        ("app_settings", AppSettings),
        ("nodes", Node),
        ("providers", ProviderDefinition),
        ("hermes_versions", HermesVersion),
        ("users", User),
        ("secrets", Secret),
        ("agents", Agent),
        ("agent_assignments", AgentAssignment),
        ("messaging_channels", MessagingChannel),
        ("scheduled_tasks", ScheduledTask),
        ("agent_templates", AgentTemplate),
        ("integration_drafts", IntegrationDraft),
    )
    OPTIONAL_TABLES: tuple[tuple[str, type, str], ...] = (
        ("tasks", Task, "include_task_history"),
        ("conversation_threads", ConversationThread, "include_task_history"),
        ("agent_messages", AgentMessage, "include_task_history"),
        ("activity_logs", ActivityLog, "include_activity_logs"),
        ("terminal_sessions", TerminalSession, "include_terminal_sessions"),
    )

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.settings = get_settings()

    async def create_backup_archive(self, payload: InstanceBackupCreateRequest) -> tuple[Path, str, InstanceBackupSummary]:
        if not payload.passphrase.strip():
            raise InstanceBackupError("Backup passphrase is required")

        archive_dir = self.settings.workspaces_root / "_backups"
        archive_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = Path(
            tempfile.NamedTemporaryFile(
                prefix="hermeshq-backup-",
                suffix=".zip",
                dir=archive_dir,
                delete=False,
            ).name
        )

        database_payload = await self._collect_database_rows(payload)
        secret_rows = await self._collect_secret_rows()
        workspace_map = await self._build_workspace_map()
        summary = self._build_summary(database_payload, payload, workspace_map)

        try:
            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                self._write_json(
                    archive,
                    "manifest.json",
                    self._manifest_payload(summary, workspace_map),
                )
                for table_name, rows in database_payload.items():
                    self._write_json(archive, f"database/{table_name}.json", rows)
                encrypted_secrets = self._encrypt_payload({"secrets": secret_rows}, payload.passphrase)
                self._write_json(archive, ENCRYPTED_SECRETS_PATH, encrypted_secrets)
                self._zip_directory(archive, self.settings.branding_root, "files/branding")
                self._zip_directory(archive, self.settings.hermes_skins_root, "files/hermes_skins")
                self._zip_directory(archive, self.settings.agent_assets_root, "files/agent_assets")
                self._zip_directory(archive, self.settings.user_assets_root, "files/user_assets")
                self._zip_directory(archive, self.settings.integration_packages_root, "files/integration_packages")
                self._zip_directory(
                    archive,
                    self.settings.workspaces_root / "_integration_factory",
                    "files/integration_factory",
                )
                for agent_id, item in workspace_map.items():
                    source = Path(item["source_path"])
                    target = f"files/workspaces/{item['archive_dir']}"
                    exclude = [] if payload.include_messaging_sessions else [
                        ".hermes/sessions",
                        ".hermes/whatsapp/session",
                    ]
                    self._zip_directory(archive, source, target, exclude_prefixes=exclude)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        filename = f"hermeshq-backup-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip"
        return tmp_path, filename, summary

    async def validate_backup_archive(self, archive_path: Path, passphrase: str | None = None) -> InstanceBackupValidationRead:
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                manifest = self._read_json(archive, "manifest.json")
                summary = self._summary_from_manifest(manifest)
                errors = self._validate_archive_members(archive, summary)
                decrypted_sections: list[str] = []
                if passphrase:
                    encrypted_secrets = self._read_json(archive, ENCRYPTED_SECRETS_PATH)
                    self._decrypt_payload(encrypted_secrets, passphrase)
                    decrypted_sections.append("secrets")
                return InstanceBackupValidationRead(
                    valid=not errors,
                    filename=archive_path.name,
                    summary=summary,
                    decrypted_sections=decrypted_sections,
                    errors=errors,
                )
        except InvalidToken as exc:
            return InstanceBackupValidationRead(
                valid=False,
                filename=archive_path.name,
                errors=["Backup passphrase could not decrypt the encrypted sections."],
            )
        except Exception as exc:
            return InstanceBackupValidationRead(
                valid=False,
                filename=archive_path.name,
                errors=[str(exc)],
            )

    async def restore_backup_archive(self, archive_path: Path, passphrase: str, mode: str, app_state) -> InstanceBackupRestoreRead:
        if mode not in {"replace", "merge"}:
            raise InstanceBackupError("Restore mode must be 'replace' or 'merge'")
        payload = await self._load_restore_payload(archive_path, passphrase)
        try:
            async with self.session_factory() as session:
                if mode == "replace":
                    await self._clear_existing_state(session, payload.summary.options)
                restored_counts = await self._restore_database(session, payload, mode)
                await session.commit()
                await self._normalize_runtime_state(session)
                await session.commit()
            self._restore_file_roots(payload, mode)
            warnings = await self._post_restore_sync(app_state)
            return InstanceBackupRestoreRead(
                restored=True,
                mode=mode,
                summary=payload.summary,
                restored_counts=restored_counts,
                warnings=warnings,
            )
        finally:
            shutil.rmtree(payload.extracted_root, ignore_errors=True)

    async def _collect_database_rows(self, payload: InstanceBackupCreateRequest) -> dict[str, list[dict[str, Any]]]:
        async with self.session_factory() as session:
            data: dict[str, list[dict[str, Any]]] = {}
            for table_name, model in self.CORE_TABLES:
                rows = (await session.execute(select(model))).scalars().all()
                if model is Secret:
                    continue
                data[table_name] = [self._serialize_row(row) for row in rows]
            for table_name, model, option_name in self.OPTIONAL_TABLES:
                if not getattr(payload, option_name):
                    continue
                rows = (await session.execute(select(model))).scalars().all()
                data[table_name] = [self._serialize_row(row) for row in rows]
            return data

    async def _collect_secret_rows(self) -> list[dict[str, Any]]:
        vault = self._secret_vault()
        async with self.session_factory() as session:
            rows = (await session.execute(select(Secret))).scalars().all()
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "provider": row.provider,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                    "value": vault.decrypt(row.value_enc),
                }
                for row in rows
            ]

    async def _build_workspace_map(self) -> dict[str, dict[str, str]]:
        async with self.session_factory() as session:
            agents = (await session.execute(select(Agent))).scalars().all()
        mapping: dict[str, dict[str, str]] = {}
        for agent in agents:
            source = Path(agent.workspace_path)
            if not source.exists():
                continue
            archive_dir = source.name
            mapping[agent.id] = {
                "archive_dir": archive_dir,
                "source_path": str(source),
                "restore_path": str((self.settings.workspaces_root / archive_dir).resolve()),
            }
        return mapping

    def _build_summary(
        self,
        database_payload: dict[str, list[dict[str, Any]]],
        payload: InstanceBackupCreateRequest,
        workspace_map: dict[str, dict[str, str]],
    ) -> InstanceBackupSummary:
        included_sections = [
            "database",
            "secrets",
            "branding",
            "skins",
            "agent_assets",
            "user_assets",
            "integration_packages",
            "integration_factory",
            "workspaces",
        ]
        counts = {table: len(rows) for table, rows in database_payload.items()}
        counts["workspaces"] = len(workspace_map)
        return InstanceBackupSummary(
            schema_version=BACKUP_SCHEMA_VERSION,
            app_version=get_app_version(),
            created_at=datetime.now(timezone.utc),
            source_hostname=socket.gethostname(),
            source_instance_root=str(self.settings.workspaces_root.parent),
            included_sections=included_sections,
            counts=counts,
            options={
                "include_activity_logs": payload.include_activity_logs,
                "include_task_history": payload.include_task_history,
                "include_terminal_sessions": payload.include_terminal_sessions,
                "include_messaging_sessions": payload.include_messaging_sessions,
            },
            warnings=[],
            encrypted_sections=["secrets"],
        )

    def _manifest_payload(self, summary: InstanceBackupSummary, workspace_map: dict[str, dict[str, str]]) -> dict[str, Any]:
        return {
            "schema_version": summary.schema_version,
            "app_version": summary.app_version,
            "created_at": summary.created_at.isoformat(),
            "source_hostname": summary.source_hostname,
            "source_instance_root": summary.source_instance_root,
            "included_sections": summary.included_sections,
            "counts": summary.counts,
            "options": summary.options,
            "warnings": summary.warnings,
            "encrypted_sections": summary.encrypted_sections,
            "workspace_map": workspace_map,
        }

    def _summary_from_manifest(self, manifest: dict[str, Any]) -> InstanceBackupSummary:
        created_at = manifest.get("created_at")
        if not isinstance(created_at, str):
            raise InstanceBackupError("Backup manifest is missing created_at")
        return InstanceBackupSummary(
            schema_version=str(manifest.get("schema_version") or ""),
            app_version=str(manifest.get("app_version") or ""),
            created_at=datetime.fromisoformat(created_at),
            source_hostname=str(manifest.get("source_hostname") or ""),
            source_instance_root=str(manifest.get("source_instance_root") or ""),
            included_sections=list(manifest.get("included_sections") or []),
            counts=dict(manifest.get("counts") or {}),
            options=dict(manifest.get("options") or {}),
            warnings=list(manifest.get("warnings") or []),
            encrypted_sections=list(manifest.get("encrypted_sections") or []),
        )

    def _validate_archive_members(self, archive: zipfile.ZipFile, summary: InstanceBackupSummary) -> list[str]:
        errors: list[str] = []
        members = set(archive.namelist())
        required = {
            "manifest.json",
            "database/app_settings.json",
            "database/nodes.json",
            "database/providers.json",
            "database/hermes_versions.json",
            "database/users.json",
            "database/agents.json",
            "database/agent_assignments.json",
            "database/messaging_channels.json",
            "database/scheduled_tasks.json",
            "database/agent_templates.json",
            "database/integration_drafts.json",
            ENCRYPTED_SECRETS_PATH,
        }
        for name in sorted(required):
            if name not in members:
                errors.append(f"Backup archive is missing required member '{name}'.")
        if summary.schema_version != BACKUP_SCHEMA_VERSION:
            errors.append(
                f"Backup schema '{summary.schema_version}' is not supported by this HermesHQ version."
            )
        return errors

    async def _load_restore_payload(self, archive_path: Path, passphrase: str) -> RestorePayload:
        extracted_root = Path(tempfile.mkdtemp(prefix="hermeshq-restore-"))
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                for member in archive.infolist():
                    target = (extracted_root / member.filename).resolve()
                    if extracted_root.resolve() not in [target, *target.parents]:
                        raise InstanceBackupError(f"Backup archive contains an invalid path '{member.filename}'")
                archive.extractall(extracted_root)
                manifest = json.loads((extracted_root / "manifest.json").read_text(encoding="utf-8"))
                summary = self._summary_from_manifest(manifest)
                validation = self._validate_archive_members(archive, summary)
                if validation:
                    raise InstanceBackupError("; ".join(validation))
            secret_blob = json.loads((extracted_root / ENCRYPTED_SECRETS_PATH).read_text(encoding="utf-8"))
            secrets = self._decrypt_payload(secret_blob, passphrase).get("secrets") or []
            database: dict[str, list[dict[str, Any]]] = {}
            database_root = extracted_root / "database"
            for path in sorted(database_root.glob("*.json")):
                database[path.stem] = json.loads(path.read_text(encoding="utf-8"))
            workspace_map = dict(manifest.get("workspace_map") or {})
            return RestorePayload(
                summary=summary,
                database=database,
                secret_rows=list(secrets),
                extracted_root=extracted_root,
                workspace_map=workspace_map,
            )
        except Exception:
            shutil.rmtree(extracted_root, ignore_errors=True)
            raise

    async def _clear_existing_state(self, session: AsyncSession, options: dict[str, Any]) -> None:
        for model in (
            ActivityLog,
            TerminalSession,
            AgentMessage,
            ConversationThread,
            Task,
            ScheduledTask,
            MessagingChannel,
            AgentAssignment,
            IntegrationDraft,
            AgentTemplate,
            Agent,
            Secret,
            User,
            ProviderDefinition,
            HermesVersion,
            Node,
            AppSettings,
        ):
            await session.execute(delete(model))

    async def _restore_database(
        self,
        session: AsyncSession,
        payload: RestorePayload,
        mode: str,
    ) -> dict[str, int]:
        restored_counts: dict[str, int] = {}
        for table_name, model in self.CORE_TABLES:
            if model is Secret:
                continue
            rows = payload.database.get(table_name, [])
            restored_counts[table_name] = await self._upsert_rows(session, model, rows, mode)
        restored_counts["secrets"] = await self._restore_secrets(session, payload.secret_rows, mode)
        for table_name, model, option_name in self.OPTIONAL_TABLES:
            if not payload.summary.options.get(option_name):
                continue
            rows = payload.database.get(table_name, [])
            restored_counts[table_name] = await self._upsert_rows(session, model, rows, mode)
        await self._rewrite_restored_workspace_paths(session, payload.workspace_map)
        return restored_counts

    async def _restore_secrets(self, session: AsyncSession, rows: list[dict[str, Any]], mode: str) -> int:
        vault = self._secret_vault()
        count = 0
        for row in rows:
            payload = dict(row)
            value = str(payload.pop("value", "") or "")
            payload["value_enc"] = vault.encrypt(value)
            secret = await session.get(Secret, payload["id"])
            if secret is None:
                session.add(Secret(**self._deserialize_payload(Secret, payload)))
            else:
                if mode == "merge":
                    for key, value in self._deserialize_payload(Secret, payload).items():
                        setattr(secret, key, value)
                else:
                    for key, value in self._deserialize_payload(Secret, payload).items():
                        setattr(secret, key, value)
            count += 1
        return count

    async def _rewrite_restored_workspace_paths(self, session: AsyncSession, workspace_map: dict[str, dict[str, str]]) -> None:
        for agent_id, item in workspace_map.items():
            agent = await session.get(Agent, agent_id)
            if agent:
                agent.workspace_path = item["restore_path"]

    async def _upsert_rows(self, session: AsyncSession, model: type, rows: list[dict[str, Any]], mode: str) -> int:
        pk_name = model.__mapper__.primary_key[0].name
        count = 0
        for row in rows:
            payload = self._deserialize_payload(model, row)
            existing = await session.get(model, payload[pk_name])
            if existing is None:
                session.add(model(**payload))
            else:
                for key, value in payload.items():
                    setattr(existing, key, value)
            count += 1
        return count

    async def _normalize_runtime_state(self, session: AsyncSession) -> None:
        agents = (await session.execute(select(Agent))).scalars().all()
        for agent in agents:
            agent.status = "stopped"
        channels = (await session.execute(select(MessagingChannel))).scalars().all()
        for channel in channels:
            channel.status = "stopped"

    def _restore_file_roots(self, payload: RestorePayload, mode: str) -> None:
        files_root = payload.extracted_root / "files"
        self._restore_directory(files_root / "branding", self.settings.branding_root, mode)
        self._restore_directory(files_root / "hermes_skins", self.settings.hermes_skins_root, mode)
        self._restore_directory(files_root / "agent_assets", self.settings.agent_assets_root, mode)
        self._restore_directory(files_root / "user_assets", self.settings.user_assets_root, mode)
        self._restore_directory(files_root / "integration_packages", self.settings.integration_packages_root, mode)
        self._restore_directory(
            files_root / "integration_factory",
            self.settings.workspaces_root / "_integration_factory",
            mode,
        )
        workspaces_root = files_root / "workspaces"
        if workspaces_root.exists():
            self.settings.workspaces_root.mkdir(parents=True, exist_ok=True)
            if mode == "replace":
                for item in self.settings.workspaces_root.iterdir():
                    if item.name.startswith("_"):
                        continue
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
            for item in workspaces_root.iterdir():
                if not item.is_dir():
                    continue
                self._restore_directory(item, self.settings.workspaces_root / item.name, mode)

    async def _post_restore_sync(self, app_state) -> list[str]:
        warnings: list[str] = []
        pty_manager = getattr(app_state, "pty_manager", None)
        if pty_manager is not None:
            for agent_id in list(pty_manager.sessions.keys()):
                await pty_manager.destroy_session(agent_id)

        hermes_version_manager = getattr(app_state, "hermes_version_manager", None)
        if hermes_version_manager is not None:
            async with self.session_factory() as session:
                versions = (await session.execute(select(HermesVersion))).scalars().all()
            for version in versions:
                if version.version == "bundled" or not version.release_tag:
                    continue
                try:
                    await hermes_version_manager.install_version(version.version)
                except Exception as exc:
                    warnings.append(f"Failed to install Hermes runtime '{version.version}': {exc}")

        installation_manager = getattr(app_state, "installation_manager", None)
        if installation_manager is not None:
            async with self.session_factory() as session:
                agents = (await session.execute(select(Agent).where(Agent.is_archived.is_(False)))).scalars().all()
            for agent in agents:
                try:
                    await installation_manager.sync_agent_installation(agent)
                except Exception as exc:
                    warnings.append(f"Failed to sync restored agent '{agent.slug}': {exc}")
        return warnings

    def _serialize_row(self, row: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        mapper = sa_inspect(type(row)).mapper
        for attr in mapper.column_attrs:
            column = attr.columns[0]
            value = getattr(row, attr.key)
            if isinstance(value, datetime):
                payload[column.name] = value.isoformat()
            elif isinstance(value, bytes):
                payload[column.name] = base64.b64encode(value).decode("ascii")
            else:
                payload[column.name] = value
        if isinstance(row, Secret):
            payload.pop("value_enc", None)
        return payload

    def _deserialize_payload(self, model: type, payload: dict[str, Any]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        mapper = sa_inspect(model).mapper
        for attr in mapper.column_attrs:
            column = attr.columns[0]
            value = payload.get(column.name)
            if value is None:
                continue
            python_type = getattr(column.type, "python_type", None)
            if python_type is datetime and isinstance(value, str):
                parsed[attr.key] = datetime.fromisoformat(value)
            elif python_type is bytes and isinstance(value, str):
                parsed[attr.key] = base64.b64decode(value.encode("ascii"))
            else:
                parsed[attr.key] = value
        return parsed

    def _write_json(self, archive: zipfile.ZipFile, name: str, payload: Any) -> None:
        archive.writestr(name, json.dumps(payload, ensure_ascii=False, indent=2))

    def _read_json(self, archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
        return json.loads(archive.read(name).decode("utf-8"))

    def _zip_directory(
        self,
        archive: zipfile.ZipFile,
        source_root: Path,
        archive_root: str,
        *,
        exclude_prefixes: list[str] | None = None,
    ) -> None:
        if not source_root.exists():
            return
        exclude_prefixes = [item.strip("/").replace("\\", "/") for item in (exclude_prefixes or [])]
        for path in sorted(source_root.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(source_root).as_posix()
            if any(rel == prefix or rel.startswith(f"{prefix}/") for prefix in exclude_prefixes):
                continue
            archive.write(path, arcname=f"{archive_root}/{rel}")

    def _restore_directory(self, source: Path, destination: Path, mode: str) -> None:
        if mode == "replace" and destination.exists():
            shutil.rmtree(destination)
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination, dirs_exist_ok=mode == "merge")
        elif mode == "replace":
            destination.mkdir(parents=True, exist_ok=True)

    def _encrypt_payload(self, payload: dict[str, Any], passphrase: str) -> dict[str, str]:
        salt = os.urandom(16)
        token = self._build_fernet(passphrase, salt).encrypt(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        return {
            "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
            "token": token.decode("ascii"),
        }

    def _decrypt_payload(self, payload: dict[str, str], passphrase: str) -> dict[str, Any]:
        salt = base64.urlsafe_b64decode(str(payload["salt"]).encode("ascii"))
        token = str(payload["token"]).encode("ascii")
        text = self._build_fernet(passphrase, salt).decrypt(token).decode("utf-8")
        return json.loads(text)

    def _build_fernet(self, passphrase: str, salt: bytes) -> Fernet:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=390000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
        return Fernet(key)

    def _secret_vault(self):
        from hermeshq.services.secret_vault import SecretVault

        return SecretVault(self.settings.fernet_key or self.settings.jwt_secret)
