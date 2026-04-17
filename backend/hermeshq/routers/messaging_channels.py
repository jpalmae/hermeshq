from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.core.security import ensure_agent_access, get_current_user, is_admin
from hermeshq.database import get_db_session
from hermeshq.models.agent import Agent
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.secret import Secret
from hermeshq.models.user import User
from hermeshq.schemas.messaging_channel import (
    MessagingChannelRead,
    MessagingChannelRuntimeRead,
    MessagingChannelUpdate,
)
from hermeshq.services.hermes_installation import HermesInstallationError
from hermeshq.models.activity import ActivityLog

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


async def _secret_exists(db: AsyncSession, secret_ref: str | None) -> bool:
    if not secret_ref:
        return False
    result = await db.execute(select(Secret.id).where(Secret.name == secret_ref))
    return result.scalar_one_or_none() is not None


def _channel_details(
    platform: str,
    secret_ref: str | None,
    extra: dict | None = None,
) -> dict:
    details = {
        "platform": platform,
        "secret_ref": secret_ref,
    }
    if extra:
        details.update(extra)
    return details


async def _log_channel_event(
    db: AsyncSession,
    agent: Agent,
    event_type: str,
    message: str,
    *,
    severity: str = "info",
    details: dict | None = None,
) -> None:
    db.add(
        ActivityLog(
            agent_id=agent.id,
            node_id=agent.node_id,
            event_type=event_type,
            severity=severity,
            message=message,
            details=details or {},
        )
    )


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
    previous_secret_ref = channel.secret_ref
    normalized_secret_ref = payload.secret_ref.strip() if payload.secret_ref else None
    secret_exists = await _secret_exists(db, normalized_secret_ref)

    if platform == "telegram":
        if payload.enabled and not normalized_secret_ref:
            await _log_channel_event(
                db,
                agent,
                "channel.telegram.config_rejected",
                f"{agent.name} telegram configuration rejected",
                severity="warning",
                details=_channel_details(platform, normalized_secret_ref, {"reason": "missing_secret_ref"}),
            )
            await db.commit()
            raise HTTPException(status_code=400, detail="Telegram bot token secret is required")
        if (
            normalized_secret_ref
            and not secret_exists
            and (payload.enabled or normalized_secret_ref != previous_secret_ref)
        ):
            await _log_channel_event(
                db,
                agent,
                "channel.telegram.config_rejected",
                f"{agent.name} telegram configuration rejected",
                severity="warning",
                details=_channel_details(platform, normalized_secret_ref, {"reason": "secret_not_found"}),
            )
            await db.commit()
            raise HTTPException(
                status_code=400,
                detail=f"Telegram bot token secret '{normalized_secret_ref}' was not found",
            )

    channel.enabled = bool(payload.enabled)
    channel.mode = payload.mode or "bidirectional"
    channel.secret_ref = normalized_secret_ref
    channel.allowed_user_ids = _normalize_string_list(payload.allowed_user_ids)
    channel.home_chat_id = payload.home_chat_id.strip() if payload.home_chat_id else None
    channel.home_chat_name = payload.home_chat_name.strip() if payload.home_chat_name else None
    channel.require_mention = bool(payload.require_mention)
    channel.free_response_chat_ids = _normalize_string_list(payload.free_response_chat_ids)
    channel.unauthorized_dm_behavior = payload.unauthorized_dm_behavior or "pair"

    await db.commit()
    await db.refresh(channel)
    try:
        await request.app.state.installation_manager.sync_agent_installation(agent)
        if channel.enabled:
            await request.app.state.gateway_supervisor.restart_channel(agent_id, platform)
        else:
            await request.app.state.gateway_supervisor.stop_channel(agent_id, platform)
    except (HermesInstallationError, ValueError) as exc:
        channel.status = "error" if channel.enabled else "stopped"
        channel.last_error = str(exc)
        await _log_channel_event(
            db,
            agent,
            "channel.telegram.config_updated",
            f"{agent.name} telegram configuration updated",
            severity="warning",
            details=_channel_details(
                platform,
                channel.secret_ref,
                {"enabled": channel.enabled, "apply_error": str(exc)},
            ),
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _log_channel_event(
        db,
        agent,
        "channel.telegram.config_updated",
        f"{agent.name} telegram configuration updated",
        details=_channel_details(platform, channel.secret_ref, {"enabled": channel.enabled}),
    )
    await db.commit()

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
    try:
        await request.app.state.gateway_supervisor.start_channel(agent_id, platform)
    except (HermesInstallationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
