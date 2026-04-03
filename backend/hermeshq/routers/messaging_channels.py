from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.user import User
from hermeshq.schemas.messaging_channel import (
    MessagingChannelRead,
    MessagingChannelRuntimeRead,
    MessagingChannelUpdate,
)

router = APIRouter(prefix="/agents/{agent_id}/channels", tags=["messaging-channels"])
SUPPORTED_PLATFORMS = {"telegram"}


async def _get_or_create_channel(
    db: AsyncSession,
    agent_id: str,
    platform: str,
) -> MessagingChannel:
    result = await db.execute(
        select(MessagingChannel).where(
            MessagingChannel.agent_id == agent_id,
            MessagingChannel.platform == platform,
        )
    )
    channel = result.scalar_one_or_none()
    if channel:
        return channel
    channel = MessagingChannel(agent_id=agent_id, platform=platform)
    db.add(channel)
    await db.flush()
    return channel


def _normalize_string_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


@router.get("", response_model=list[MessagingChannelRead])
async def list_channels(
    agent_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[MessagingChannelRead]:
    await ensure_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(MessagingChannel)
        .where(MessagingChannel.agent_id == agent_id)
        .order_by(MessagingChannel.platform.asc())
    )
    return [MessagingChannelRead.model_validate(item) for item in result.scalars().all()]


@router.put("/{platform}", response_model=MessagingChannelRead)
async def upsert_channel(
    agent_id: str,
    platform: str,
    payload: MessagingChannelUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> MessagingChannelRead:
    agent = await ensure_agent_access(db, current_user, agent_id)
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unsupported platform")
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Only admins can modify messaging channels")

    channel = await _get_or_create_channel(db, agent_id, platform)
    channel.enabled = bool(payload.enabled)
    channel.mode = payload.mode or "bidirectional"
    channel.secret_ref = payload.secret_ref.strip() if payload.secret_ref else None
    channel.allowed_user_ids = _normalize_string_list(payload.allowed_user_ids)
    channel.home_chat_id = payload.home_chat_id.strip() if payload.home_chat_id else None
    channel.home_chat_name = payload.home_chat_name.strip() if payload.home_chat_name else None
    channel.require_mention = bool(payload.require_mention)
    channel.free_response_chat_ids = _normalize_string_list(payload.free_response_chat_ids)
    channel.unauthorized_dm_behavior = payload.unauthorized_dm_behavior or "pair"
    if channel.enabled and platform == "telegram" and not channel.secret_ref:
        raise HTTPException(status_code=400, detail="Telegram bot token secret is required")

    await db.commit()
    await db.refresh(channel)
    await request.app.state.installation_manager.sync_agent_installation(agent)
    if channel.enabled:
        await request.app.state.gateway_supervisor.restart_channel(agent_id, platform)
    else:
        await request.app.state.gateway_supervisor.stop_channel(agent_id, platform)

    result = await db.execute(
        select(MessagingChannel).where(
            MessagingChannel.agent_id == agent_id,
            MessagingChannel.platform == platform,
        )
    )
    return MessagingChannelRead.model_validate(result.scalar_one())


@router.get("/{platform}", response_model=MessagingChannelRead)
async def get_channel(
    agent_id: str,
    platform: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> MessagingChannelRead:
    await ensure_agent_access(db, current_user, agent_id)
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unsupported platform")
    channel = await _get_or_create_channel(db, agent_id, platform)
    await db.commit()
    await db.refresh(channel)
    return MessagingChannelRead.model_validate(channel)


@router.get("/{platform}/runtime", response_model=MessagingChannelRuntimeRead)
async def get_channel_runtime(
    agent_id: str,
    platform: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> MessagingChannelRuntimeRead:
    await ensure_agent_access(db, current_user, agent_id)
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unsupported platform")
    runtime = await request.app.state.gateway_supervisor.get_runtime_status(agent_id, platform)
    return MessagingChannelRuntimeRead(**runtime)


@router.post("/{platform}/start", response_model=MessagingChannelRuntimeRead)
async def start_channel(
    agent_id: str,
    platform: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> MessagingChannelRuntimeRead:
    await ensure_agent_access(db, current_user, agent_id)
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unsupported platform")
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Only admins can start messaging channels")
    await request.app.state.gateway_supervisor.start_channel(agent_id, platform)
    runtime = await request.app.state.gateway_supervisor.get_runtime_status(agent_id, platform)
    return MessagingChannelRuntimeRead(**runtime)


@router.post("/{platform}/stop", response_model=MessagingChannelRuntimeRead)
async def stop_channel(
    agent_id: str,
    platform: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> MessagingChannelRuntimeRead:
    await ensure_agent_access(db, current_user, agent_id)
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unsupported platform")
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Only admins can stop messaging channels")
    await request.app.state.gateway_supervisor.stop_channel(agent_id, platform)
    runtime = await request.app.state.gateway_supervisor.get_runtime_status(agent_id, platform)
    return MessagingChannelRuntimeRead(**runtime)


@router.get("/{platform}/logs")
async def get_channel_logs(
    agent_id: str,
    platform: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    await ensure_agent_access(db, current_user, agent_id)
    if platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=404, detail="Unsupported platform")
    logs = await request.app.state.gateway_supervisor.tail_log(agent_id, platform)
    return {"platform": platform, "content": logs}
