"""
Webhook endpoints for external platform integrations.

Receives incoming events from Google Chat and Kapso WhatsApp
and routes them to the appropriate gateway instances.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jose import jwt as jose_jwt
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.database import get_db_session

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# Google Chat signs webhook requests with a JWT from this service account.
_GOOGLE_CHAT_ISSUER = "chat@system.gserviceaccount.com"


def _verify_google_chat_bearer(request: Request) -> None:
    """Reject requests that lack a Google-issued Bearer JWT.

    Full RS256 signature verification requires fetching Google's public certs
    at runtime. Here we perform a lightweight guard: require the Authorization
    header to be present and check that the unverified issuer claim matches
    Google Chat's well-known service account. This blocks unauthenticated
    scanners while keeping the endpoint functional without network round-trips.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Google Chat bearer token")
    token = auth_header[len("Bearer "):]
    try:
        claims = jose_jwt.get_unverified_claims(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid Google Chat bearer token")
    if claims.get("iss") != _GOOGLE_CHAT_ISSUER:
        logger.warning(
            "Google Chat webhook: unexpected token issuer %r", claims.get("iss")
        )
        raise HTTPException(status_code=401, detail="Untrusted Google Chat token issuer")


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
    _verify_google_chat_bearer(request)
    try:
        payload = await request.json()
    except (json.JSONDecodeError, TypeError, ValueError):
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


# ---------------------------------------------------------------------------
# Kapso WhatsApp webhook
# ---------------------------------------------------------------------------


@router.post("/webhooks/kapso-whatsapp")
async def kapso_whatsapp_webhook(
    request: Request,
) -> dict:
    """
    Receive incoming events from Kapso WhatsApp platform.

    Kapso sends webhook events for:
    - whatsapp.message.received — new message from customer
    - whatsapp.message.sent — message sent confirmation
    - whatsapp.message.delivered — delivery confirmation
    - whatsapp.message.read — read receipt
    - whatsapp.message.failed — delivery failure
    - whatsapp.conversation.created / ended / inactive
    """
    # Read raw body for signature verification
    body = await request.body()

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Kapso webhook: invalid JSON payload")
        return {"error": "invalid payload"}

    # Normalize to list — Kapso may send a single event (dict) or a batch (list)
    raw_events: list[dict] = payload if isinstance(payload, list) else [payload]

    # Get Kapso gateways from app state
    kapso_gateways = getattr(request.app.state, "kapso_gateways", {})
    if not kapso_gateways:
        logger.warning("Kapso webhook received but no gateways registered")
        return {"status": "ok"}

    # Verify webhook signature if available
    signature = request.headers.get("X-Webhook-Signature", "")

    from hermeshq.services.kapso_whatsapp_gateway import (
        handle_kapso_webhook,
        verify_webhook_signature,
    )

    # Verify signature using raw body (before parsing)
    gateways_with_secret = [gw for gw in kapso_gateways.values() if gw._webhook_secret]
    if gateways_with_secret:
        if not signature:
            logger.warning("Kapso webhook: missing X-Webhook-Signature header — rejecting")
            return Response(status_code=401, content="Missing webhook signature")
        verified = any(
            verify_webhook_signature(body, signature, gw._webhook_secret)
            for gw in gateways_with_secret
        )
        if not verified:
            logger.warning("Kapso webhook: signature verification failed")
            return Response(status_code=401, content="Invalid signature")
    elif signature:
        logger.warning("Kapso webhook: signature present but no webhook_secret configured — rejecting")
        return Response(status_code=401, content="Webhook secret not configured")

    # Process each event (single or batch)
    for event_data in raw_events:
        event_type = event_data.get("event", "")
        data = event_data.get("data", event_data)

        if not event_type:
            event_type = request.headers.get("X-Webhook-Event", "")
            if not event_type:
                logger.warning("Kapso webhook: skipping event without type")
                continue

        await handle_kapso_webhook(
            event_type=event_type,
            payload=data if data is not event_data else event_data,
            gateways=kapso_gateways,
        )

    return {"status": "ok"}
