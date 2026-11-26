import logging
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.models.task import Task
from hermeshq.services.task_board import next_board_order, runtime_status_to_board_column

logger = logging.getLogger(__name__)

IDENTITY_TTL = timedelta(hours=24)
_LOOKBACK = timedelta(hours=26)


def new_identity_code() -> str:
    return secrets.token_hex(3).upper()


def _expired(iso: str | None) -> bool:
    if not iso:
        return True
    try:
        exp = datetime.fromisoformat(iso)
    except ValueError:
        return True
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return datetime.now(UTC) > exp


async def approve_delegation_identity(
    db: AsyncSession, agent_id: str, user_id: str, code: str
) -> tuple[str | None, str | None]:
    since = datetime.now(UTC) - _LOOKBACK
    result = await db.execute(
        select(Task).where(Task.agent_id == agent_id, Task.status == "failed", Task.queued_at >= since)
    )
    code_up = (code or "").upper()
    match = None
    for task in result.scalars().all():
        meta = task.metadata_json or {}
        if not meta.get("delegated") or str(meta.get("delegation_owner_user_id") or "") != str(user_id):
            continue
        if not getattr(task, "source_agent_id", None) or getattr(task, "created_by_user_id", None):
            continue
        if str(meta.get("identity_approval_code") or "").upper() != code_up:
            continue
        if _expired(meta.get("identity_approval_expires")):
            continue
        match = task
        break
    if match is None:
        return None, None
    meta = match.metadata_json or {}
    consumido = {**meta, "identity_approval_code": None, "identity_approved": True}
    consumo = await db.execute(
        update(Task)
        .where(
            Task.id == match.id,
            Task.metadata_json["identity_approval_code"].as_string() == str(meta.get("identity_approval_code")),
        )
        .values(metadata_json=consumido)
        .returning(Task.id)
        .execution_options(synchronize_session=False)
    )
    if consumo.scalar_one_or_none() is None:
        logger.info("Código de delegación de la tarea %s ya consumido por otra petición", match.id)
        return None, None
    titulo = str(meta.get("title") or match.title or "la solicitud")[:200]
    nuevo = Task(
        agent_id=match.agent_id,
        source_agent_id=match.source_agent_id,
        parent_task_id=match.parent_task_id,
        created_by_user_id=None,
        title=match.title,
        prompt=match.prompt,
        metadata_json={
            "delegated": True,
            "title": meta.get("title"),
            "thread_user_id": str(user_id),
            "identity_granted": True,
            "origin_task_id": match.id,
        },
    )
    nuevo.board_column = runtime_status_to_board_column(nuevo.status)
    nuevo.board_order = next_board_order()
    nuevo.board_manual = False
    db.add(nuevo)
    match.metadata_json = consumido
    await db.flush()
    reply = f"✅ Autorizado. Reintento «{titulo}» con tu cuenta y te aviso del resultado por aquí."
    return nuevo.id, reply
