import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, false, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from hermeshq.core.security import (
    ensure_agent_access,
    get_accessible_agent_ids,
    get_current_user,
    is_admin,
    require_admin,
)
from hermeshq.database import get_db_session
from hermeshq.models.conversation_thread import ConversationThread
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.schemas.task import TaskBoardUpdate, TaskCreate, TaskListRead, TaskQueueStateRead, TaskRead
from hermeshq.services.metadata_cliente import metadata_cliente_segura
from hermeshq.services.task_board import is_valid_board_column, next_board_order, runtime_status_to_board_column
from hermeshq.services.task_visibility import task_visibility_clause, task_visible_to_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskListRead])
async def list_tasks(
    agent_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[TaskListRead]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    statement = (
        select(Task)
        .options(defer(Task.messages_json), defer(Task.tool_calls))
        .order_by(desc(Task.queued_at))
        .limit(limit)
        .offset(offset)
    )
    if agent_id:
        statement = statement.where(Task.agent_id == agent_id)
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        if agent_id and agent_id not in accessible_ids:
            raise HTTPException(status_code=403, detail="Access denied")
        if not accessible_ids:
            statement = statement.where(false())
        else:
            statement = statement.where(
                Task.agent_id.in_(accessible_ids),
                task_visibility_clause(current_user.id),
            )
    result = await db.execute(statement)
    return [TaskListRead.model_validate(t) for t in result.scalars().all()]


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TaskRead:
    agent = await ensure_agent_access(db, current_user, payload.agent_id)
    if agent.is_archived:
        raise HTTPException(status_code=400, detail="Archived agents cannot receive new tasks")
    payload_data = payload.model_dump()
    metadata = metadata_cliente_segura(payload_data.pop("metadata", None))
    metadata["thread_user_id"] = current_user.id
    inferred_conversation = (payload.title or "").strip() == "Chat message"
    if inferred_conversation and not metadata.get("conversation"):
        metadata["conversation"] = True
        metadata.setdefault("source", "agent_conversation")
    thread = None
    if metadata.get("conversation"):
        result = await db.execute(
            select(ConversationThread).where(
                ConversationThread.agent_id == payload.agent_id,
                ConversationThread.user_id == current_user.id,
            )
        )
        thread = result.scalar_one_or_none()
        if not thread:
            thread = ConversationThread(
                agent_id=payload.agent_id,
                user_id=current_user.id,
                title=(payload.title or payload.prompt[:80]).strip() or "Conversation",
            )
            db.add(thread)
            await db.flush()
        metadata["thread_id"] = thread.id
        from hermeshq.services.aprobacion_central import aprobar_texto_si_corresponde

        aprobada = await aprobar_texto_si_corresponde(
            db, payload.agent_id, current_user.id, thread.id, payload.prompt, "web"
        )
        if aprobada:
            metadata["aprobacion_id"] = aprobada
        from hermeshq.services import ejecucion_aprobada

        aviso = await ejecucion_aprobada.avisos_pendientes(db, payload.agent_id, current_user.id, thread.id)
        if aviso:
            metadata["aprobacion_aviso"] = aviso
    elif metadata.get("thread_id"):
        propio = await db.get(ConversationThread, metadata["thread_id"])
        if not propio or propio.agent_id != payload.agent_id or propio.user_id != current_user.id:
            metadata.pop("thread_id", None)
    task = Task(**payload_data, metadata_json=metadata, created_by_user_id=current_user.id)
    task.board_column = runtime_status_to_board_column(task.status)
    task.board_order = next_board_order()
    task.board_manual = False
    db.add(task)
    await db.flush()
    if thread:
        thread.last_task_id = task.id
    await db.commit()
    await db.refresh(task)
    if agent.status == "running":
        await request.app.state.supervisor.submit_task(task.id)
    return TaskRead.model_validate(task)


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TaskRead:
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await ensure_agent_access(db, current_user, task.agent_id)
    if not is_admin(current_user) and not task_visible_to_user(task, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    return TaskRead.model_validate(task)


@router.post("/{task_id}/cancel", response_model=TaskRead)
async def cancel_task(
    task_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TaskRead:
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await ensure_agent_access(db, current_user, task.agent_id)
    if not is_admin(current_user) and not task_visible_to_user(task, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    await request.app.state.supervisor.cancel_task(task_id)
    await db.refresh(task)
    return TaskRead.model_validate(task)


@router.patch("/{task_id}/board", response_model=TaskRead)
async def update_task_board(
    task_id: str,
    payload: TaskBoardUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> TaskRead:
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await ensure_agent_access(db, current_user, task.agent_id)
    if not is_admin(current_user) and not task_visible_to_user(task, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    if not is_valid_board_column(payload.board_column):
        raise HTTPException(status_code=400, detail="Invalid board column")
    task.board_column = payload.board_column
    task.board_order = payload.board_order or next_board_order()
    task.board_manual = True
    await db.commit()
    await db.refresh(task)
    return TaskRead.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    task = (await db.execute(select(Task).where(Task.id == task_id).with_for_update())).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await ensure_agent_access(db, current_user, task.agent_id)
    if not is_admin(current_user) and not task_visible_to_user(task, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied")
    if task.status == "running":
        raise HTTPException(status_code=409, detail="Cannot delete a running task. Cancel it first.")

    # Clean up attachment files from disk before removing the DB record.
    workspace_manager = getattr(request.app.state, "workspace_manager", None)
    if workspace_manager:
        try:
            workspace: Path = workspace_manager.build_workspace_path(task.agent_id)
            workspace_root = str(workspace.resolve())
            for att in (task.metadata_json or {}).get("attachments", []):
                rel = att.get("path", "")
                if not rel:
                    continue
                file_path = (workspace / rel).resolve()
                if not str(file_path).startswith(workspace_root + os.sep) and str(file_path) != workspace_root:
                    logger.warning("Skipping attachment path outside workspace: %s", rel)
                    continue
                if file_path.is_file():
                    file_path.unlink()
        except Exception:
            logger.warning("Could not clean up attachments for task %s", task_id, exc_info=True)

    await db.delete(task)
    await db.commit()


@router.get("/queue/state", response_model=TaskQueueStateRead)
async def queue_state(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    accessible_ids = await get_accessible_agent_ids(db, current_user)
    queued_statement = select(func.count()).select_from(Task).where(Task.status == "queued")
    running_statement = select(func.count()).select_from(Task).where(Task.status == "running")
    if not is_admin(current_user):
        if not accessible_ids:
            queued_statement = queued_statement.where(false())
            running_statement = running_statement.where(false())
        else:
            queued_statement = queued_statement.where(
                Task.agent_id.in_(accessible_ids), Task.created_by_user_id == current_user.id
            )
            running_statement = running_statement.where(
                Task.agent_id.in_(accessible_ids), Task.created_by_user_id == current_user.id
            )
    queued_count = (await db.execute(queued_statement)).scalar() or 0
    running_count = (await db.execute(running_statement)).scalar() or 0
    return {
        "queued": queued_count,
        "running": running_count,
    }


class ExternalActionReconcile(BaseModel):
    ejecutada: bool
    nota: str | None = Field(default=None, max_length=500)


@router.get("/{task_id}/external-actions")
async def list_external_actions(
    task_id: str,
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    rows = (
        (
            await db.execute(
                text(
                    "SELECT id, attempt_id, lease_generation, kind, state, detail, resolved_by, created_at, "
                    "finished_at FROM external_action_records WHERE task_id = :t ORDER BY created_at"
                ),
                {"t": task_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.post("/{task_id}/external-actions/{action_id}/reconcile")
async def reconcile_task_external_action(
    task_id: str,
    action_id: str,
    payload: ExternalActionReconcile,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    from hermeshq.services.external_actions import reconcile_external_action

    if not await reconcile_external_action(db, task_id, action_id, payload.ejecutada, admin.id, payload.nota):
        raise HTTPException(status_code=409, detail="La acción no existe o ya está conciliada")
    return {"id": action_id, "estado": "conciliada_ejecutada" if payload.ejecutada else "conciliada_no_ejecutada"}
