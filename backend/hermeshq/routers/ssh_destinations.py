from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import get_current_user, require_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.ssh_destination import SshDestination
from hermeshq.models.user import User
from hermeshq.schemas.ssh_destination import SshDestinationCreate, SshDestinationRead, SshDestinationUpdate
from hermeshq.services.audit import record_audit
from hermeshq.services.ssh_relay import reconcile_ssh_relays

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ssh-destinations", tags=["ssh-destinations"])


async def _validate_agent(db: AsyncSession, agent_id: str | None) -> None:
    if not agent_id:
        return
    agent = await db.get(Agent, agent_id)
    if not agent or agent.is_archived:
        raise HTTPException(status_code=404, detail="Agent not found")


@router.get("", response_model=list[SshDestinationRead])
async def list_ssh_destinations(
    include_inactive: bool = False,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[SshDestinationRead]:
    statement = select(SshDestination).order_by(SshDestination.created_at.asc())
    if not include_inactive:
        statement = statement.where(SshDestination.active.is_(True))
    result = await db.execute(statement)
    return [SshDestinationRead.model_validate(item) for item in result.scalars().all()]


@router.post("", response_model=SshDestinationRead, status_code=201)
async def create_ssh_destination(
    payload: SshDestinationCreate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> SshDestinationRead:
    await _validate_agent(db, payload.allowed_agent_id)
    item = SshDestination(
        id=str(uuid4()),
        name=payload.name.strip(),
        host=payload.host.strip(),
        port=payload.port,
        listen_port=payload.listen_port,
        allowed_agent_id=payload.allowed_agent_id,
        active=True,
        notes=payload.notes,
        created_by_user_id=current_user.id,
    )
    db.add(item)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Destination host/port already exists") from exc
    await db.refresh(item)
    await record_audit(
        db,
        action="ssh_destination.created",
        target_type="ssh_destination",
        target_id=item.id,
        details={
            "host": item.host,
            "port": item.port,
            "listen_port": item.listen_port,
            "allowed_agent_id": item.allowed_agent_id,
        },
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )
    try:
        await reconcile_ssh_relays(request.app, db)
    except Exception:
        logger.exception("Failed to reconcile ssh relays after create")
    return SshDestinationRead.model_validate(item)


@router.put("/{destination_id}", response_model=SshDestinationRead)
async def update_ssh_destination(
    destination_id: str,
    payload: SshDestinationUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> SshDestinationRead:
    item = await db.get(SshDestination, destination_id)
    if not item:
        raise HTTPException(status_code=404, detail="Destination not found")
    changes = payload.model_dump(exclude_unset=True)
    if "allowed_agent_id" in changes:
        await _validate_agent(db, changes["allowed_agent_id"])
    for field, value in changes.items():
        setattr(item, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Destination host/port already exists") from exc
    await db.refresh(item)
    await record_audit(
        db,
        action="ssh_destination.updated",
        target_type="ssh_destination",
        target_id=item.id,
        details={"changes": changes},
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )
    try:
        await reconcile_ssh_relays(request.app, db)
    except Exception:
        logger.exception("Failed to reconcile ssh relays after update")
    return SshDestinationRead.model_validate(item)


@router.delete("/{destination_id}", status_code=204)
async def delete_ssh_destination(
    destination_id: str,
    request: Request,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
) -> None:
    item = await db.get(SshDestination, destination_id)
    if not item:
        raise HTTPException(status_code=404, detail="Destination not found")
    await db.delete(item)
    await db.commit()
    await record_audit(
        db,
        action="ssh_destination.deleted",
        target_type="ssh_destination",
        target_id=destination_id,
        details={"host": item.host, "port": item.port},
        actor_id=current_user.id,
        actor_username=current_user.username,
        actor_role=current_user.role,
    )
    try:
        await reconcile_ssh_relays(request.app, db)
    except Exception:
        logger.exception("Failed to reconcile ssh relays after delete")
