from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import desc, false, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_accessible_agent_ids, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.conversation_thread import ConversationThread
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.schemas.task import TaskBoardUpdate, TaskCreate, TaskRead
from hermeshq.services.task_board import is_valid_board_column, next_board_order, runtime_status_to_board_column

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[TaskRead]:
    statement = select(Task).order_by(desc(Task.queued_at))
    if not is_admin(current_user):
        accessible_ids = await get_accessible_agent_ids(db, current_user)
        statement = statement.where(Task.agent_id.in_(accessible_ids)) if accessible_ids else statement.where(false())
    result = await db.execute(statement)
    return [TaskRead.model_validate(task) for task in result.scalars().all()]


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
    metadata = payload_data.pop("metadata", {}) or {}
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
        metadata["thread_user_id"] = current_user.id
    task = Task(**payload_data, metadata_json=metadata)
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
    if not is_valid_board_column(payload.board_column):
        raise HTTPException(status_code=400, detail="Invalid board column")
    task.board_column = payload.board_column
    task.board_order = payload.board_order or next_board_order()
    task.board_manual = True
    await db.commit()
    await db.refresh(task)
    return TaskRead.model_validate(task)


@router.get("/queue/state")
async def queue_state(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    accessible_ids = await get_accessible_agent_ids(db, current_user)
    queued_statement = select(Task).where(Task.status == "queued")
    running_statement = select(Task).where(Task.status == "running")
    if not is_admin(current_user):
        queued_statement = queued_statement.where(Task.agent_id.in_(accessible_ids)) if accessible_ids else queued_statement.where(false())
        running_statement = running_statement.where(Task.agent_id.in_(accessible_ids)) if accessible_ids else running_statement.where(false())
    queued_result = await db.execute(queued_statement)
    running_result = await db.execute(running_statement)
    return {
        "queued": len(queued_result.scalars().all()),
        "running": len(running_result.scalars().all()),
    }
