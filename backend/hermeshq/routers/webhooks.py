"""
Webhook endpoints for Microsoft Teams and Google Chat.

These endpoints receive incoming events from the respective platforms
and route them to the appropriate gateway instance.
"""

import json
import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.database import get_db_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


# ---------------------------------------------------------------------------
# Microsoft Teams webhook
# ---------------------------------------------------------------------------


@router.post("/webhooks/teams")
async def teams_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict | None:
    """
    Receive incoming events from Microsoft Teams Bot Framework.

    Teams sends events to this endpoint when:
    - A user sends a message to the bot
    - The bot is added/removed from a conversation
    - A card interaction occurs
    """
    try:
        payload = await request.json()
    except Exception:
        return {"error": "invalid payload"}

    # Get the Teams gateways from app state
    gateways = getattr(request.app.state, "teams_gateways", {})
    if not gateways:
        logger.warning("Teams webhook received but no gateways registered")
        return {"status": "ok"}

    from hermeshq.services.teams_gateway import handle_teams_webhook

    result = await handle_teams_webhook(
        payload=payload,
        session_factory=request.app.state.session_factory,
        gateways=gateways,
    )
    return result or {"status": "ok"}


# ---------------------------------------------------------------------------
# Google Chat webhook
# ---------------------------------------------------------------------------


@router.post("/webhooks/google-chat")
async def google_chat_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict | None:
    """
    Receive incoming events from Google Chat.

    Google Chat sends events to this endpoint when:
    - A user sends a message to the bot
    - The bot is added/removed from a space
    - A card interaction occurs
    """
    try:
        payload = await request.json()
    except Exception:
        return {"error": "invalid payload"}

    gateways = getattr(request.app.state, "google_chat_gateways", {})
    if not gateways:
        logger.warning("Google Chat webhook received but no gateways registered")
        return {"status": "ok"}

    from hermeshq.services.google_chat_gateway import handle_google_chat_webhook

    result = await handle_google_chat_webhook(
        payload=payload,
        session_factory=request.app.state.session_factory,
        gateways=gateways,
    )
    return result or {"status": "ok"}
