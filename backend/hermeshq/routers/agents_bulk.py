"""Agent bulk operation endpoints – bulk task dispatch, bulk message send."""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user
from hermeshq.database import get_db_session
from hermeshq.models.task import Task
from hermeshq.models.user import User
from hermeshq.routers.agents_shared import (
    _auto_start_agent_if_needed,
    _create_conversation_task,
    _load_bulk_agents,
    _load_enabled_integration_slugs,
    _sync_agent_integration_toolsets,
)
from hermeshq.schemas.agent import (
    AgentBulkConfigUpdate,
    AgentBulkMessageCreate,
    AgentBulkOperationResult,
    AgentBulkOperationSkipped,
    AgentBulkTaskCreate,
)
from hermeshq.services.audit import extract_ip, record_audit
from hermeshq.services.runtime_profiles import normalize_runtime_profile_slug
from hermeshq.services.task_board import next_board_order, runtime_status_to_board_column

logger = logging.getLogger(__name__)
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


@router.post("/bulk/config", response_model=AgentBulkOperationResult, status_code=status.HTTP_200_OK)
async def bulk_config_update(
    payload: AgentBulkConfigUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> AgentBulkOperationResult:
    """Apply configuration changes to multiple agents at once.

    Only non-None fields from the payload are applied. Archived agents are skipped.
    """
    from hermeshq.core.security import ensure_agent_access, is_admin

    update_data = payload.model_dump(exclude_unset=True, exclude={"agent_ids"})
    if not update_data:
        raise HTTPException(status_code=400, detail="No configuration fields provided")

    agents = await _load_bulk_agents(db, current_user, payload.agent_ids)
    submitted_agent_ids: list[str] = []
    skipped_agents: list[AgentBulkOperationSkipped] = []
    enabled_integration_slugs = await _load_enabled_integration_slugs(db)

    for agent in agents:
        if agent.is_archived:
            skipped_agents.append(AgentBulkOperationSkipped(agent_id=agent.id, reason="archived"))
            continue
        if not is_admin(current_user):
            try:
                await ensure_agent_access(db, current_user, agent.id)
            except HTTPException:
                skipped_agents.append(AgentBulkOperationSkipped(agent_id=agent.id, reason="no access"))
                continue
            # Enforce field-level authorization — same check as single-agent update
            from hermeshq.routers.agents_shared import USER_EDITABLE_FIELDS
            restricted_fields = sorted(set(update_data) - USER_EDITABLE_FIELDS)
            if restricted_fields:
                skipped_agents.append(AgentBulkOperationSkipped(
                    agent_id=agent.id,
                    reason=f"restricted fields: {', '.join(restricted_fields)}",
                ))
                continue

        runtime_profile_changed = "runtime_profile" in update_data

        if "runtime_profile" in update_data:
            update_data["runtime_profile"] = normalize_runtime_profile_slug(update_data["runtime_profile"])

        if "model" in update_data or "provider" in update_data or "api_key_ref" in update_data or "base_url" in update_data:
            update_data.setdefault("use_provider_default", False)

        for field, value in update_data.items():
            setattr(agent, field, value)

        if runtime_profile_changed:
            from hermeshq.routers.agents_shared import _apply_runtime_profile_defaults
            _apply_runtime_profile_defaults(
                agent,
                agent.runtime_profile,
                overwrite_toolsets="enabled_toolsets" not in update_data and "disabled_toolsets" not in update_data,
            )
        _sync_agent_integration_toolsets(agent, enabled_integration_slugs)

        submitted_agent_ids.append(agent.id)

    if not submitted_agent_ids:
        raise HTTPException(status_code=400, detail="No valid agents were available for bulk config update")

    # Record audit entry in the same transaction as the config changes so that
    # both land atomically — a failed commit cannot leave data without an audit trail.
    await record_audit(
        db,
        action="agent.bulk_config",
        target_type="agent",
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
        ip_address=extract_ip(request),
        new_value=update_data,
        details={"agent_count": len(submitted_agent_ids), "agent_ids": submitted_agent_ids},
    )
    await db.commit()

    # Sync installations for updated agents
    installation_manager = request.app.state.installation_manager
    for agent_id in submitted_agent_ids:
        agent = next((a for a in agents if a.id == agent_id), None)
        if agent:
            await installation_manager.sync_agent_installation(agent)

    return AgentBulkOperationResult(
        batch_id=None,
        submitted=len(submitted_agent_ids),
        skipped=len(skipped_agents),
        submitted_agent_ids=submitted_agent_ids,
        skipped_agents=skipped_agents,
        task_ids=[],
    )
