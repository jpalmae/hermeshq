from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.capability_request import CapabilityRequest
from hermeshq.models.user import User
from hermeshq.schemas.capability_request import CAPABILITY_STATUSES, CapabilityRequestRead, CapabilityRequestUpdate

router = APIRouter(prefix="/capability-requests", tags=["capability-requests"])


def _require_admin(user: User) -> None:
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Solo un administrador puede ver las solicitudes de capacidades")


async def _view(db: AsyncSession, item: CapabilityRequest) -> CapabilityRequestRead:
    agent = await db.get(Agent, item.agent_id)
    user = await db.get(User, item.requested_by_user_id) if item.requested_by_user_id else None
    data = CapabilityRequestRead.model_validate(item)
    data.agent_name = (agent.friendly_name or agent.name) if agent else None
    data.requested_by_name = (getattr(user, "full_name", None) or getattr(user, "username", None)) if user else None
    return data


@router.get("", response_model=list[CapabilityRequestRead])
async def list_capability_requests(
    status: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[CapabilityRequestRead]:
    _require_admin(current_user)
    statement = select(CapabilityRequest).order_by(CapabilityRequest.created_at.desc()).limit(200)
    if status:
        statement = statement.where(CapabilityRequest.status == status)
    result = await db.execute(statement)
    return [await _view(db, item) for item in result.scalars().all()]


@router.patch("/{request_id}", response_model=CapabilityRequestRead)
async def update_capability_request(
    request_id: str,
    payload: CapabilityRequestUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> CapabilityRequestRead:
    _require_admin(current_user)
    item = await db.get(CapabilityRequest, request_id)
    if not item:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    if payload.status is not None:
        if payload.status not in CAPABILITY_STATUSES:
            raise HTTPException(status_code=400, detail=f"status debe ser uno de: {', '.join(CAPABILITY_STATUSES)}")
        item.status = payload.status
        item.resolved_at = datetime.now(UTC) if payload.status in ("resuelto", "descartado") else None
    if payload.resolution is not None:
        item.resolution = payload.resolution.strip()[:3000] or None
    await db.commit()
    await db.refresh(item)
    return await _view(db, item)
