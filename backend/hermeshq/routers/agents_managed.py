"""Agent managed integration endpoints – test and action execution."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.secret import Secret
from hermeshq.models.user import User
from hermeshq.schemas.managed_integration import (
    ManagedIntegrationActionRequest,
    ManagedIntegrationActionResult,
    ManagedIntegrationTestRequest,
    ManagedIntegrationTestResult,
)
from hermeshq.services.managed_integration_actions import ManagedIntegrationActionError, run_managed_integration_action
from hermeshq.services.managed_integration_health import ManagedIntegrationTestError, test_managed_integration

from hermeshq.routers.agents_shared import _load_enabled_integration_slugs
from hermeshq.models.activity import ActivityLog

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/{agent_id}/integrations/{integration_slug}/test", response_model=ManagedIntegrationTestResult)
async def test_agent_integration(
    agent_id: str,
    integration_slug: str,
    payload: ManagedIntegrationTestRequest,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> ManagedIntegrationTestResult:
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    async def _resolve_secret(secret_ref: str) -> str | None:
        result = await db.execute(select(Secret).where(Secret.name == secret_ref))
        secret = result.scalar_one_or_none()
        if not secret:
            return None
        return request.app.state.secret_vault.decrypt(secret.value_enc)

    try:
        enabled_integration_slugs = await _load_enabled_integration_slugs(db)
        success, message, details = await test_managed_integration(
            agent,
            integration_slug,
            payload.config or {},
            enabled_integration_slugs,
            _resolve_secret,
        )
        return ManagedIntegrationTestResult(success=success, message=message, details=details)
    except ManagedIntegrationTestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{agent_id}/integrations/{integration_slug}/actions/{action_slug}", response_model=ManagedIntegrationActionResult)
async def run_agent_integration_action(
    agent_id: str,
    integration_slug: str,
    action_slug: str,
    payload: ManagedIntegrationActionRequest,
    request: Request,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> ManagedIntegrationActionResult:
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    async def _resolve_secret(secret_ref: str) -> str | None:
        result = await db.execute(select(Secret).where(Secret.name == secret_ref))
        secret = result.scalar_one_or_none()
        if not secret:
            return None
        return request.app.state.secret_vault.decrypt(secret.value_enc)

    try:
        enabled_integration_slugs = await _load_enabled_integration_slugs(db)
        success, message, details = await run_managed_integration_action(
            agent,
            integration_slug,
            action_slug,
            payload.config or {},
            enabled_integration_slugs,
            _resolve_secret,
        )
    except ManagedIntegrationActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    issue_count = 0
    if isinstance(details, dict):
        issue_count = int(details.get("issue_count") or 0)
    severity = "error" if not success else "warning" if issue_count else "info"
    db.add(
        ActivityLog(
            agent_id=agent.id,
            event_type=f"security.{integration_slug}.{action_slug}",
            severity=severity,
            message=message,
            details={
                "integration_slug": integration_slug,
                "action_slug": action_slug,
                "success": success,
                "summary": details,
            },
        )
    )
    await db.commit()
    return ManagedIntegrationActionResult(success=success, message=message, details=details)
