from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

ATTEMPT_HEADER = "X-HermesHQ-Attempt-ID"
GENERATION_HEADER = "X-HermesHQ-Lease-Generation"
DEDUP_CONFIRMED_SECONDS = 600
UNRESOLVED_STATES = ("iniciada",)


class ExternalActionDenied(PermissionError):
    def __init__(self, motivo: str, status_code: int = 403, detalle: dict | None = None):
        super().__init__(motivo)
        self.motivo = motivo
        self.status_code = status_code
        self.detalle = detalle or {}


@dataclass(frozen=True)
class AttemptRef:
    attempt_id: str
    generation: int
    task_id: str


def attempt_from_headers(headers) -> tuple[str, int] | None:
    attempt_id = str(headers.get(ATTEMPT_HEADER, "") or "").strip()
    raw_generation = str(headers.get(GENERATION_HEADER, "") or "").strip()
    if not attempt_id or not raw_generation:
        return None
    try:
        return attempt_id, int(raw_generation)
    except ValueError:
        return None


def fingerprint_for(user_id: str, kind: str, payload) -> str:
    material = json.dumps({"u": user_id, "k": kind, "p": payload}, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def require_live_attempt(
    db: AsyncSession, headers, *, agent_id: str, task_id: str | None = None, lock: bool = False
) -> AttemptRef:
    parsed = attempt_from_headers(headers)
    if parsed is None:
        raise ExternalActionDenied("intento_no_identificado")
    attempt_id, generation = parsed
    row = (
        (
            await db.execute(
                text(
                    "SELECT a.attempt_id, a.task_id, a.state, a.lease_generation, "
                    "a.lease_expires_at > now() AS vigente "
                    "FROM task_execution_attempts a JOIN tasks t ON t.id = a.task_id "
                    "WHERE a.attempt_id = :a AND t.agent_id = :agent" + (" FOR SHARE OF a" if lock else "")
                ),
                {"a": attempt_id, "agent": agent_id},
            )
        )
        .mappings()
        .first()
    )
    if (
        row is None
        or row["state"] != "running"
        or int(row["lease_generation"]) != generation
        or not row["vigente"]
        or (task_id is not None and row["task_id"] != task_id)
    ):
        logger.warning(
            "Acción externa denegada: intento %s (generación %s) no vigente para el agente %s",
            attempt_id,
            generation,
            agent_id,
        )
        raise ExternalActionDenied("intento_no_vigente")
    return AttemptRef(attempt_id=attempt_id, generation=generation, task_id=row["task_id"])


async def unresolved_actions(db: AsyncSession, task_id: str) -> list[dict]:
    rows = (
        (
            await db.execute(
                text(
                    "SELECT id, attempt_id, kind, state, created_at FROM external_action_records "
                    "WHERE task_id = :t AND state = 'iniciada' ORDER BY created_at"
                ),
                {"t": task_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def begin_external_action(
    db: AsyncSession, headers, *, agent_id: str, task_id: str, user_id: str, kind: str, payload
) -> str:
    ref = await require_live_attempt(db, headers, agent_id=agent_id, task_id=task_id, lock=True)
    fingerprint = fingerprint_for(user_id, kind, payload)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:f))"), {"f": fingerprint})
    pendientes = await unresolved_actions(db, task_id)
    if pendientes:
        raise ExternalActionDenied("conciliacion_requerida", 409, {"acciones": [str(p["id"]) for p in pendientes]})
    previa = (
        (
            await db.execute(
                text(
                    "SELECT id, state FROM external_action_records WHERE fingerprint = :f AND ("
                    "state = 'iniciada' OR (state IN ('confirmada', 'conciliada_ejecutada') "
                    "AND created_at > now() - make_interval(secs => :ventana))) LIMIT 1"
                ),
                {"f": fingerprint, "ventana": DEDUP_CONFIRMED_SECONDS},
            )
        )
        .mappings()
        .first()
    )
    if previa is not None:
        motivo = "conciliacion_requerida" if previa["state"] == "iniciada" else "accion_ya_ejecutada"
        raise ExternalActionDenied(motivo, 409, {"accion": str(previa["id"])})
    record_id = str(uuid4())
    await db.execute(
        text(
            "INSERT INTO external_action_records (id, task_id, attempt_id, lease_generation, kind, fingerprint, "
            "state, created_at) VALUES (:id, :t, :a, :g, :k, :f, 'iniciada', now())"
        ),
        {"id": record_id, "t": task_id, "a": ref.attempt_id, "g": ref.generation, "k": kind[:160], "f": fingerprint},
    )
    await db.commit()
    return record_id


async def finish_external_action(db: AsyncSession, record_id: str, state: str, detail: str | None = None) -> None:
    if state not in ("confirmada", "no_ejecutada"):
        raise ValueError(state)
    await db.execute(
        text(
            "UPDATE external_action_records SET state = :s, detail = :d, finished_at = now() "
            "WHERE id = :id AND state = 'iniciada'"
        ),
        {"s": state, "d": (detail or "")[:500] or None, "id": record_id},
    )
    await db.commit()


async def reconcile_external_action(
    db: AsyncSession, task_id: str, record_id: str, executed: bool, user_id: str, note: str | None = None
) -> bool:
    row = (
        await db.execute(
            text(
                "UPDATE external_action_records SET state = :s, resolved_by = :u, "
                "detail = COALESCE(:d, detail), finished_at = now() "
                "WHERE id = :id AND task_id = :t AND state = 'iniciada' RETURNING id"
            ),
            {
                "s": "conciliada_ejecutada" if executed else "conciliada_no_ejecutada",
                "u": user_id,
                "d": (note or "")[:500] or None,
                "id": record_id,
                "t": task_id,
            },
        )
    ).first()
    await db.commit()
    return row is not None
