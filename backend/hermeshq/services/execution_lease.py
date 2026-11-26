from __future__ import annotations

import asyncio
import json
import logging
import socket
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.task import Task
from hermeshq.services.efectos_herramientas import (
    EVENTO_ACTIVACION_HERRAMIENTA,
    EVENTO_FIN_HERRAMIENTA,
    EVENTO_INICIO_HERRAMIENTA,
    efectos_posibles,
)

logger = logging.getLogger(__name__)

INSTANCE_OWNER = f"{socket.gethostname()}:{uuid4()}"
CLAIM_WINDOW_SECONDS = 60

_ALLOWED_TRANSITIONS = {
    ("pending", "running"),
    ("pending", "failed"),
    ("pending", "cancelled"),
    ("running", "completed"),
    ("running", "failed"),
    ("running", "cancelled"),
}

RECONCILE_CLASSES = (
    "viva_autorizada",
    "viva_sin_titularidad",
    "interrumpida",
    "inaccesible",
    "resultado_incierto",
    "recuperable",
    "obsoleto",
)


def lease_settings() -> dict:
    from hermeshq.config import get_settings

    s = get_settings()
    return validate_lease_settings(
        {
            "ttl": int(s.execution_lease_ttl_seconds),
            "renew_interval": int(s.execution_lease_renew_interval_seconds),
            "heartbeat_interval": int(s.runner_heartbeat_interval_seconds),
            "heartbeat_stale": int(s.runner_heartbeat_stale_seconds),
            "reconcile_interval": int(s.execution_reconcile_interval_seconds),
            "reconcile_grace": int(s.execution_reconcile_grace_seconds),
        }
    )


def validate_lease_settings(cfg: dict) -> dict:
    if cfg["renew_interval"] < 1 or cfg["heartbeat_interval"] < 1 or cfg["reconcile_interval"] < 1:
        raise ValueError("Los intervalos de lease, latido y reconciliación deben ser >= 1 s")
    if cfg["reconcile_grace"] < 0:
        raise ValueError("La gracia de reconciliación no puede ser negativa")
    if cfg["heartbeat_stale"] <= cfg["heartbeat_interval"]:
        raise ValueError("runner_heartbeat_stale_seconds debe superar runner_heartbeat_interval_seconds")
    if cfg["ttl"] <= cfg["heartbeat_stale"] or cfg["ttl"] <= cfg["renew_interval"]:
        raise ValueError(
            "execution_lease_ttl_seconds debe superar runner_heartbeat_stale_seconds y "
            "execution_lease_renew_interval_seconds, o el lease vence antes de detectar un runner detenido"
        )
    return dict(cfg)


def entrega_tras_reencolar(metadata: dict) -> dict:
    metadata = dict(metadata or {})
    if metadata.get("source") == "public_chat" and not metadata.get("scheduled") and not metadata.get("background_job"):
        metadata["respuesta_tardia"] = True
    return metadata


def mensaje_de_fallo(clase: str) -> str:
    if clase == "interrumpida":
        return (
            "La tarea se interrumpió antes de ejecutar acciones externas (reinicio o pérdida del proceso). "
            "No se repitió nada: puedes volver a pedirla."
        )
    if clase == "resultado_incierto":
        return (
            "La ejecución se perdió después de usar herramientas que pueden hacer cambios (enviar, guardar, programar "
            "o delegar), así que puede que alguno sí se haya hecho. No se reintentó para evitar duplicados: revisa "
            "antes de volver a pedirla."
        )
    return f"La ejecución quedó sin titular y no se reintenta automáticamente ({clase.replace('_', ' ')})."


async def clasificar_interrupcion(session: AsyncSession, task: Task) -> tuple[str, list[dict]]:
    metadata = dict(task.metadata_json or {})
    acciones = await ExecutionReconciler._actions_of(session, task_id=task.id)
    ultimo = (
        (
            await session.execute(
                text(
                    "SELECT attempt_id, state, tool_events, reconciliation FROM task_execution_attempts "
                    "WHERE task_id = :t ORDER BY created_at DESC LIMIT 1"
                ),
                {"t": task.id},
            )
        )
        .mappings()
        .first()
    )
    incierta = (
        bool(metadata.get("acciones_externas"))
        or bool(acciones)
        or (
            ultimo is not None
            and (
                ultimo["state"] == "completed"
                or ultimo["reconciliation"] == "resultado_incierto"
                or efectos_posibles(
                    int(ultimo["tool_events"] or 0),
                    await ExecutionReconciler._started_tools(session, task.id, ultimo["attempt_id"]),
                )
            )
        )
    )
    await session.execute(
        text(
            "UPDATE task_execution_attempts SET state = 'abandoned', lease_expires_at = now(), "
            "lease_generation = lease_generation + 1, reconciliation = :clase, end_reason = 'reconciliado', "
            "finished_at = now(), updated_at = now() WHERE task_id = :t AND state IN ('pending', 'running')"
        ),
        {"t": task.id, "clase": "resultado_incierto" if incierta else "interrumpida"},
    )
    return ("resultado_incierto" if incierta else "interrumpida"), acciones


class ExecutionLeaseError(RuntimeError):
    pass


class LeaseNotAcquiredError(ExecutionLeaseError):
    pass


class AttemptConflictError(ExecutionLeaseError):
    pass


class LeaseLostError(ExecutionLeaseError):
    pass


class IdentityUnconfirmedError(ExecutionLeaseError):
    pass


class ReconciliationRequiredError(ExecutionLeaseError):
    pass


@dataclass(frozen=True)
class Lease:
    execution_id: str
    attempt_id: str
    task_id: str
    attempt_number: int
    owner: str
    generation: int
    expires_at: datetime

    def identity(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "attempt_id": self.attempt_id,
            "lease_generation": self.generation,
        }

    def matches(self, echo: dict | None) -> bool:
        if not isinstance(echo, dict):
            return False
        return (
            echo.get("execution_id") == self.execution_id
            and echo.get("attempt_id") == self.attempt_id
            and echo.get("lease_generation") == self.generation
        )


@dataclass
class LocalRunner:
    lease: Lease
    process: asyncio.subprocess.Process
    fenced_reason: str | None = None


@dataclass
class LocalRunnerRegistry:
    runners: dict[str, LocalRunner] = field(default_factory=dict)

    def register(self, lease: Lease, process: asyncio.subprocess.Process) -> LocalRunner:
        entry = LocalRunner(lease=lease, process=process)
        self.runners[lease.attempt_id] = entry
        return entry

    def unregister(self, attempt_id: str) -> None:
        self.runners.pop(attempt_id, None)

    def get(self, attempt_id: str) -> LocalRunner | None:
        return self.runners.get(attempt_id)

    def fence(self, attempt_id: str, reason: str) -> bool:
        entry = self.runners.get(attempt_id)
        if entry is None or entry.process.returncode is not None:
            return False
        entry.fenced_reason = entry.fenced_reason or reason
        entry.process.kill()
        return True


class ExecutionLeaseStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], owner: str = INSTANCE_OWNER) -> None:
        self.session_factory = session_factory
        self.owner = owner

    async def create_attempt(self, task_id: str, execution_id: str, attempt_number: int) -> str:
        attempt_id = str(uuid4())
        async with self.session_factory() as session:
            pendientes = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM external_action_records WHERE state = 'iniciada' AND task_id IN "
                        "(SELECT CAST(:task_id AS VARCHAR) UNION "
                        "SELECT metadata->>'retry_of' FROM tasks WHERE id = :task_id)"
                    ),
                    {"task_id": task_id},
                )
            ).scalar()
            if pendientes:
                raise ReconciliationRequiredError(
                    f"La tarea {task_id} tiene {pendientes} acción(es) externa(s) con resultado incierto; "
                    "hay que conciliarlas antes de reintentar"
                )
            try:
                await session.execute(
                    text(
                        "INSERT INTO task_execution_attempts "
                        "(attempt_id, execution_id, task_id, attempt_number, state, lease_generation, "
                        "lease_expires_at, heartbeat_count, runner_heartbeat_seq, tool_events, created_at, updated_at) "
                        "VALUES (:attempt_id, :execution_id, :task_id, :attempt_number, 'pending', 0, "
                        "now() + make_interval(secs => :claim), 0, 0, 0, now(), now())"
                    ),
                    {
                        "attempt_id": attempt_id,
                        "execution_id": execution_id,
                        "task_id": task_id,
                        "attempt_number": attempt_number,
                        "claim": CLAIM_WINDOW_SECONDS,
                    },
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise AttemptConflictError(f"La tarea {task_id} ya tiene un intento vivo") from exc
        return attempt_id

    async def acquire(self, attempt_id: str, ttl_seconds: int, owner: str | None = None) -> Lease:
        owner = owner or self.owner
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE task_execution_attempts "
                        "SET lease_owner = :owner, lease_generation = lease_generation + 1, "
                        "lease_expires_at = now() + make_interval(secs => :ttl), updated_at = now() "
                        "WHERE attempt_id = :attempt_id AND state = 'pending' "
                        "AND (lease_owner IS NULL OR lease_expires_at <= now()) "
                        "RETURNING execution_id, task_id, attempt_number, lease_generation, lease_expires_at"
                    ),
                    {"owner": owner, "ttl": int(ttl_seconds), "attempt_id": attempt_id},
                )
            ).first()
            await session.commit()
        if row is None:
            raise LeaseNotAcquiredError(f"El intento {attempt_id} ya tiene propietario o no está pendiente")
        return Lease(
            execution_id=row.execution_id,
            attempt_id=attempt_id,
            task_id=row.task_id,
            attempt_number=row.attempt_number,
            owner=owner,
            generation=int(row.lease_generation),
            expires_at=row.lease_expires_at,
        )

    async def renew(self, lease: Lease, ttl_seconds: int, runner_seq: int, tool_events: int = 0) -> datetime:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE task_execution_attempts "
                        "SET lease_expires_at = now() + make_interval(secs => :ttl), last_heartbeat_at = now(), "
                        "heartbeat_count = heartbeat_count + 1, runner_heartbeat_seq = :seq, "
                        "tool_events = GREATEST(tool_events, :tools), updated_at = now() "
                        "WHERE attempt_id = :attempt_id AND lease_owner = :owner AND lease_generation = :generation "
                        "AND state = 'running' AND lease_expires_at > now() AND runner_heartbeat_seq < :seq "
                        "RETURNING lease_expires_at"
                    ),
                    {
                        "ttl": int(ttl_seconds),
                        "seq": int(runner_seq),
                        "tools": int(tool_events),
                        "attempt_id": lease.attempt_id,
                        "owner": lease.owner,
                        "generation": lease.generation,
                    },
                )
            ).first()
            await session.commit()
        if row is None:
            raise LeaseLostError(
                f"El intento {lease.attempt_id} (generación {lease.generation}) no pudo renovar su lease"
            )
        return row.lease_expires_at

    async def registrar_inicio_herramienta(self, lease: Lease, agent_id: str | None, nombre: str) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO activity_logs (id, agent_id, task_id, event_type, severity, message, details, "
                    "created_at, updated_at) VALUES (:id, :agent, :task, :tipo, 'info', :nombre, "
                    "CAST(:detalle AS json), now(), now())"
                ),
                {
                    "id": str(uuid4()),
                    "agent": agent_id,
                    "task": lease.task_id,
                    "tipo": EVENTO_INICIO_HERRAMIENTA,
                    "nombre": str(nombre or "")[:200],
                    "detalle": json.dumps({"attempt_id": lease.attempt_id}),
                },
            )
            await session.commit()

    async def registrar_activacion_herramienta(self, lease: Lease, agent_id: str | None, nombre: str) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO activity_logs (id, agent_id, task_id, event_type, severity, message, details, "
                    "created_at, updated_at) VALUES (:id, :agent, :task, :tipo, 'info', :nombre, "
                    "CAST(:detalle AS json), now(), now())"
                ),
                {
                    "id": str(uuid4()),
                    "agent": agent_id,
                    "task": lease.task_id,
                    "tipo": EVENTO_ACTIVACION_HERRAMIENTA,
                    "nombre": str(nombre or "")[:200],
                    "detalle": json.dumps({"attempt_id": lease.attempt_id}),
                },
            )
            await session.commit()

    async def registrar_fin_herramienta(
        self,
        lease: Lease,
        agent_id: str | None,
        nombre: str,
        clase: str,
        duracion_ms: int = 0,
        auditoria: dict | None = None,
    ) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO activity_logs (id, agent_id, task_id, event_type, severity, message, details, "
                    "created_at, updated_at) VALUES (:id, :agent, :task, :tipo, :severidad, :nombre, "
                    "CAST(:detalle AS json), now(), now())"
                ),
                {
                    "id": str(uuid4()),
                    "agent": agent_id,
                    "task": lease.task_id,
                    "tipo": EVENTO_FIN_HERRAMIENTA,
                    "severidad": "warning" if clase in ("error_externo", "error_interno", "sin_resultado") else "info",
                    "nombre": str(nombre or "")[:200],
                    "detalle": json.dumps(
                        {"attempt_id": lease.attempt_id, "clase": str(clase)[:30], "ms": int(duracion_ms or 0)}
                    ),
                },
            )
            if auditoria and auditoria.get("skill") and clase == "ok":
                await session.execute(
                    text(
                        "INSERT INTO activity_logs (id, agent_id, task_id, event_type, severity, message, details, "
                        "created_at, updated_at) VALUES (:id, :agent, :task, 'skill.audit', 'info', :mensaje, "
                        "CAST(:detalle AS json), now(), now())"
                    ),
                    {
                        "id": str(uuid4()),
                        "agent": agent_id,
                        "task": lease.task_id,
                        "mensaje": f"{auditoria.get('accion')} {auditoria.get('skill')}"[:200],
                        "detalle": json.dumps(
                            {k: auditoria[k] for k in ("accion", "skill", "destinatarios") if auditoria.get(k)},
                            ensure_ascii=False,
                        ),
                    },
                )
            await session.commit()

    async def transition(
        self,
        lease: Lease,
        from_state: str,
        to_state: str,
        *,
        runner_confirmed: bool = False,
        end_reason: str | None = None,
    ) -> None:
        if (from_state, to_state) not in _ALLOWED_TRANSITIONS:
            raise ValueError(f"Transición no permitida: {from_state} -> {to_state}")
        terminal = to_state in ("completed", "failed", "cancelled")
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE task_execution_attempts SET state = :to_state, updated_at = now(), "
                        "finished_at = CASE WHEN :terminal THEN now() ELSE finished_at END, "
                        "runner_confirmed_at = CASE WHEN :confirmed THEN now() ELSE runner_confirmed_at END, "
                        "end_reason = COALESCE(CAST(:end_reason AS VARCHAR), end_reason) "
                        "WHERE attempt_id = :attempt_id AND state = :from_state "
                        "AND lease_owner = :owner AND lease_generation = :generation "
                        "AND lease_expires_at > now() "
                        "RETURNING attempt_id"
                    ),
                    {
                        "to_state": to_state,
                        "terminal": terminal,
                        "confirmed": runner_confirmed,
                        "end_reason": end_reason,
                        "attempt_id": lease.attempt_id,
                        "from_state": from_state,
                        "owner": lease.owner,
                        "generation": lease.generation,
                    },
                )
            ).first()
            await session.commit()
        if row is None:
            raise LeaseLostError(
                f"El intento {lease.attempt_id} (generación {lease.generation}) ya no es propietario; "
                f"se rechaza {from_state} -> {to_state}"
            )

    async def mark_lost(self, lease: Lease, reason: str) -> bool:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE task_execution_attempts SET state = 'lost', end_reason = :reason, "
                        "updated_at = now(), finished_at = now() "
                        "WHERE attempt_id = :attempt_id AND lease_owner = :owner "
                        "AND lease_generation = :generation AND state = 'running' RETURNING attempt_id"
                    ),
                    {
                        "reason": reason,
                        "attempt_id": lease.attempt_id,
                        "owner": lease.owner,
                        "generation": lease.generation,
                    },
                )
            ).first()
            await session.commit()
        return row is not None

    async def begin(self, task_id: str, execution_id: str, attempt_number: int, ttl_seconds: int) -> Lease:
        attempt_id = await self.create_attempt(task_id, execution_id, attempt_number)
        lease = await self.acquire(attempt_id, ttl_seconds)
        await self.transition(lease, "pending", "running")
        return lease

    async def close_quietly(self, lease: Lease, to_state: str, end_reason: str | None = None) -> bool:
        try:
            await self.transition(lease, "running", to_state, end_reason=end_reason)
            return True
        except LeaseLostError:
            logger.warning("No se pudo cerrar el intento %s como %s: ya no es propietario", lease.attempt_id, to_state)
        except Exception:  # noqa: BLE001
            logger.exception("Fallo al cerrar el intento %s como %s", lease.attempt_id, to_state)
            return False
        try:
            if await self.mark_lost(lease, end_reason or to_state):
                logger.warning("Intento %s marcado como lost (%s)", lease.attempt_id, end_reason or to_state)
        except Exception:  # noqa: BLE001
            logger.exception("Fallo al marcar como lost el intento %s", lease.attempt_id)
        return False


class ExecutionReconciler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: LocalRunnerRegistry | None = None,
        owner: str = INSTANCE_OWNER,
        grace_seconds: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry if registry is not None else LocalRunnerRegistry()
        self.owner = owner
        self.grace_seconds = grace_seconds

    def _grace(self) -> int:
        return self.grace_seconds if self.grace_seconds is not None else lease_settings()["reconcile_grace"]

    async def reconcile(self) -> dict[str, list[str]]:
        report: dict[str, list[str]] = {clase: [] for clase in RECONCILE_CLASSES}
        await self._fence_local_runners_without_title(report)
        async with self.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT attempt_id, task_id, state, lease_owner, lease_generation, tool_events, "
                            "lease_expires_at > now() AS vigente, "
                            "lease_expires_at > now() - make_interval(secs => :grace) AS en_gracia "
                            "FROM task_execution_attempts WHERE state IN ('pending', 'running', 'lost')"
                        ),
                        {"grace": self._grace()},
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            if row["state"] == "lost":
                report["obsoleto"].append(row["attempt_id"])
                continue
            if row["vigente"]:
                report["viva_autorizada"].append(row["attempt_id"])
                continue
            report["inaccesible"].append(row["attempt_id"])
            await self._annotate(row["attempt_id"], row["lease_generation"], "inaccesible")
        return report

    @staticmethod
    async def _started_tools(session: AsyncSession, task_id: str, attempt_id: str) -> list[str]:
        rows = await session.execute(
            text(
                "SELECT message FROM activity_logs WHERE task_id = :t AND event_type = :tipo "
                "AND details->>'attempt_id' = :a"
            ),
            {"t": task_id, "tipo": EVENTO_INICIO_HERRAMIENTA, "a": attempt_id},
        )
        return [str(r[0] or "") for r in rows.all()]

    @staticmethod
    async def _actions_of(session: AsyncSession, *, attempt_id: str | None = None, task_id: str | None = None):
        column, value = ("attempt_id", attempt_id) if attempt_id else ("task_id", task_id)
        rows = (
            (
                await session.execute(
                    text(
                        f"SELECT id, kind, state FROM external_action_records WHERE {column} = :v "
                        "AND state NOT IN ('no_ejecutada', 'conciliada_no_ejecutada') ORDER BY created_at"
                    ),
                    {"v": value},
                )
            )
            .mappings()
            .all()
        )
        return [{"id": r["id"], "tipo": r["kind"], "estado": r["state"]} for r in rows]

    async def _fence_local_runners_without_title(self, report: dict[str, list[str]]) -> None:
        if not self.registry.runners:
            return
        async with self.session_factory() as session:
            for attempt_id, entry in list(self.registry.runners.items()):
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT state, lease_owner, lease_generation, lease_expires_at > now() AS vigente "
                                "FROM task_execution_attempts WHERE attempt_id = :a"
                            ),
                            {"a": attempt_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                titular = (
                    row is not None
                    and row["state"] == "running"
                    and row["lease_owner"] == entry.lease.owner
                    and row["lease_generation"] == entry.lease.generation
                    and row["vigente"]
                )
                if not titular and self.registry.fence(attempt_id, "sin_titularidad"):
                    report["viva_sin_titularidad"].append(attempt_id)

    async def _annotate(self, attempt_id: str, generation: int, clase: str) -> None:
        async with self.session_factory() as session:
            await session.execute(
                text(
                    "UPDATE task_execution_attempts SET reconciliation = :clase, updated_at = now() "
                    "WHERE attempt_id = :a AND lease_generation = :g"
                ),
                {"clase": clase, "a": attempt_id, "g": generation},
            )
            await session.commit()
