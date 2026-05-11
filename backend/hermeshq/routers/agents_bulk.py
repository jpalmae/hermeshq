"""Agent bulk operation endpoints – bulk task dispatch, bulk message send."""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.schemas.agent import (
    AgentBulkMessageCreate,
    AgentBulkOperationResult,
    AgentBulkOperationSkipped,
    AgentBulkTaskCreate,
)

from hermeshq.routers.agents_shared import (
    _auto_start_agent_if_needed,
    _create_conversation_task,
    _load_bulk_agents,
)
from hermeshq.services.task_board import next_board_order, runtime_status_to_board_column

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/bulk/task", response_model=AgentBulkOperationResult, status_code=status.HTTP_201_CREATED)
async def bulk_dispatch_task(
    payload: AgentBulkTaskCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentBulkOperationResult:
    agents = await _load_bulk_agents(db, current_user, payload.agent_ids)
    batch_id = str(uuid4())
    batch_label = payload.title.strip()
    submitted_agent_ids: list[str] = []
    skipped_agents: list[AgentBulkOperationSkipped] = []
    tasks_to_submit: list[str] = []
    task_ids: list[str] = []

    for agent in agents:
        if agent.is_archived:
            skipped_agents.append(AgentBulkOperationSkipped(agent_id=agent.id, reason="archived"))
            continue
        start_error = await _auto_start_agent_if_needed(
            db,
            request,
            agent,
            auto_start_stopped=payload.auto_start_stopped,
        )
        if start_error:
            skipped_agents.append(AgentBulkOperationSkipped(agent_id=agent.id, reason=start_error))
            continue

        task = Task(
            agent_id=agent.id,
            title=batch_label,
            prompt=payload.prompt,
            priority=payload.priority,
            metadata_json={
                "batch_id": batch_id,
                "batch_label": batch_label,
                "batch_origin": "agents_bulk_dispatch",
                "batch_size": len(agents) - len(skipped_agents),
            },
        )
        task.board_column = runtime_status_to_board_column(task.status)
        task.board_order = next_board_order()
        task.board_manual = False
        db.add(task)
        await db.flush()
        task_ids.append(task.id)
        submitted_agent_ids.append(agent.id)
        if agent.status == "running":
            tasks_to_submit.append(task.id)

    if not submitted_agent_ids:
        raise HTTPException(status_code=400, detail="No valid agents were available for this bulk task")

    for task_id in task_ids:
        task = await db.get(Task, task_id)
        if task:
            metadata = dict(task.metadata_json or {})
            metadata["batch_size"] = len(submitted_agent_ids)
            task.metadata_json = metadata

    await db.commit()

    for task_id in tasks_to_submit:
        await request.app.state.supervisor.submit_task(task_id)

    return AgentBulkOperationResult(
        batch_id=batch_id,
        submitted=len(submitted_agent_ids),
        skipped=len(skipped_agents),
        submitted_agent_ids=submitted_agent_ids,
        skipped_agents=skipped_agents,
        task_ids=task_ids,
    )


@router.post("/bulk/message", response_model=AgentBulkOperationResult, status_code=status.HTTP_201_CREATED)
async def bulk_send_message(
    payload: AgentBulkMessageCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentBulkOperationResult:
    agents = await _load_bulk_agents(db, current_user, payload.agent_ids)
    campaign_id = str(uuid4())
    submitted_agent_ids: list[str] = []
    skipped_agents: list[AgentBulkOperationSkipped] = []
    tasks_to_submit: list[str] = []
    task_ids: list[str] = []

    for agent in agents:
        if agent.is_archived:
            skipped_agents.append(AgentBulkOperationSkipped(agent_id=agent.id, reason="archived"))
            continue
        start_error = await _auto_start_agent_if_needed(
            db,
            request,
            agent,
            auto_start_stopped=payload.auto_start_stopped,
        )
        if start_error:
            skipped_agents.append(AgentBulkOperationSkipped(agent_id=agent.id, reason=start_error))
            continue

        task = await _create_conversation_task(
            db,
            agent=agent,
            current_user=current_user,
            prompt=payload.message,
            metadata={
                "source": "agents_bulk_message",
                "campaign_id": campaign_id,
                "batch_size": len(agents) - len(skipped_agents),
            },
        )
        submitted_agent_ids.append(agent.id)
        task_ids.append(task.id)
        if agent.status == "running":
            tasks_to_submit.append(task.id)

    if not submitted_agent_ids:
        raise HTTPException(status_code=400, detail="No valid agents were available for this bulk message")

    for task_id in task_ids:
        task = await db.get(Task, task_id)
        if task:
            metadata = dict(task.metadata_json or {})
            metadata["batch_size"] = len(submitted_agent_ids)
            task.metadata_json = metadata

    await db.commit()

    for task_id in tasks_to_submit:
        await request.app.state.supervisor.submit_task(task_id)

    return AgentBulkOperationResult(
        batch_id=campaign_id,
        submitted=len(submitted_agent_ids),
        skipped=len(skipped_agents),
        submitted_agent_ids=submitted_agent_ids,
        skipped_agents=skipped_agents,
        task_ids=task_ids,
    )
