"""
Kapso WhatsApp Gateway — Meta Cloud API via Kapso platform.

Receives messages via Kapso webhooks and forwards them as tasks
to the agent supervisor.  When a task completes, the response
is sent back via the Kapso REST API.

Unlike the Baileys bridge (which runs as a Node.js subprocess),
this gateway runs as an asyncio task within the backend process,
enabling horizontal scalability without per-agent process overhead.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import time
import uuid

import httpx
from sqlalchemy import select

# ---------------------------------------------------------------------------
# Shared HTTP client (lazily initialized, reused across calls)
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Return a shared httpx.AsyncClient, creating one if needed."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        transport = httpx.AsyncHTTPTransport(retries=2)
        _http_client = httpx.AsyncClient(timeout=30, transport=transport)
    return _http_client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.secret import Secret
from hermeshq.models.task import Task
from hermeshq.services.channel_user_resolver import resolve_channel_user
from hermeshq.services.secret_vault import SecretVault

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kapso API constants
# ---------------------------------------------------------------------------

KAPSO_API_BASE = "https://api.kapso.ai/meta/whatsapp"
KAPSO_API_VERSION = "v24.0"

# Webhook event types we handle
EVENT_MESSAGE_RECEIVED = "whatsapp.message.received"
EVENT_MESSAGE_SENT = "whatsapp.message.sent"
EVENT_MESSAGE_DELIVERED = "whatsapp.message.delivered"
EVENT_MESSAGE_READ = "whatsapp.message.read"
EVENT_MESSAGE_FAILED = "whatsapp.message.failed"
EVENT_CONVERSATION_CREATED = "whatsapp.conversation.created"
EVENT_CONVERSATION_ENDED = "whatsapp.conversation.ended"
EVENT_CONVERSATION_INACTIVE = "whatsapp.conversation.inactive"


# ---------------------------------------------------------------------------
# Kapso REST helpers
# ---------------------------------------------------------------------------


async def kapso_send_text(
    api_key: str,
    phone_number_id: str,
    to: str,
    text: str,
) -> dict:
    """
    Send a text message via Kapso WhatsApp API.

    POST /meta/whatsapp/v24.0/{phone_number_id}/messages
    """
    url = f"{KAPSO_API_BASE}/{KAPSO_API_VERSION}/{phone_number_id}/messages"
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    client = _get_http_client()
    resp = await client.post(url, json=body, headers=headers)
    resp.raise_for_status()
    return resp.json()


async def kapso_send_media(
    api_key: str,
    phone_number_id: str,
    to: str,
    media_type: str,
    media_link: str,
    caption: str | None = None,
) -> dict:
    """
    Send a media message (image, video, audio, document) via Kapso.

    POST /meta/whatsapp/v24.0/{phone_number_id}/messages
    """
    url = f"{KAPSO_API_BASE}/{KAPSO_API_VERSION}/{phone_number_id}/messages"
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }
    media_obj: dict = {"link": media_link}
    if caption:
        media_obj["caption"] = caption
    body = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": media_type,
        media_type: media_obj,
    }
    client = _get_http_client()
    resp = await client.post(url, json=body, headers=headers)
    resp.raise_for_status()
    return resp.json()


async def kapso_mark_read(
    api_key: str,
    phone_number_id: str,
    message_id: str,
) -> dict | None:
    """Mark a message as read."""
    url = f"{KAPSO_API_BASE}/{KAPSO_API_VERSION}/{phone_number_id}/messages"
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }
    body = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        client = _get_http_client()
        resp = await client.post(url, json=body, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        logger.debug("Failed to mark message %s as read", message_id, exc_info=True)
        return None


_WA_MAX_CHARS = 4000  # WhatsApp hard limit is 4096 — use 4000 for safety margin


def _split_message(text: str) -> list[str]:
    """
    Split a long message into chunks of at most _WA_MAX_CHARS characters.

    Strategy (in order):
      1. Split on double newlines (paragraphs) — preferred break point.
      2. If a paragraph is still too long, split on single newlines.
      3. If a line is still too long, split on sentence boundaries (. ! ?).
      4. Hard-split anything that remains.
    """
    if len(text) <= _WA_MAX_CHARS:
        return [text]

    chunks: list[str] = []
    current = ""

    def flush(chunk: str) -> None:
        chunk = chunk.strip()
        if chunk:
            chunks.append(chunk)

    def add_segment(segment: str) -> None:
        nonlocal current
        if not segment:
            return
        if len(current) + len(segment) + 1 <= _WA_MAX_CHARS:
            current = (current + "\n" + segment).lstrip("\n")
        else:
            flush(current)
            current = segment

    # Walk paragraph by paragraph
    for para in re.split(r"\n{2,}", text):
        if len(para) <= _WA_MAX_CHARS:
            add_segment(para)
            continue
        # Paragraph too long — split on single newlines
        for line in para.split("\n"):
            if len(line) <= _WA_MAX_CHARS:
                add_segment(line)
                continue
            # Line too long — split on sentence boundaries
            for sentence in re.split(r"(?<=[.!?])\s+", line):
                if len(sentence) <= _WA_MAX_CHARS:
                    add_segment(sentence)
                else:
                    # Hard split
                    for i in range(0, len(sentence), _WA_MAX_CHARS):
                        add_segment(sentence[i: i + _WA_MAX_CHARS])

    flush(current)
    return chunks or [text[:_WA_MAX_CHARS]]


async def kapso_send_text_chunked(
    api_key: str,
    phone_number_id: str,
    to: str,
    text: str,
) -> None:
    """Send a (possibly long) text, splitting it into WhatsApp-safe chunks."""
    for chunk in _split_message(text):
        await kapso_send_text(api_key, phone_number_id, to, chunk)


def verify_webhook_signature(
    payload_body: bytes,
    signature_header: str,
    secret: str,
) -> bool:
    """
    Verify Kapso webhook signature using HMAC-SHA256.

    Kapso sends the signature in the X-Webhook-Signature header.
    """
    if not signature_header or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        payload_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Kapso WhatsApp Gateway
# ---------------------------------------------------------------------------


class KapsoWhatsAppGateway:
    """
    Manages a Kapso WhatsApp connection for a single HermesHQ agent.

    Runs as a lightweight coroutine within the backend process.
    Receives messages via Kapso webhooks and creates Tasks for
    the agent supervisor.
    """

    def __init__(
        self,
        agent_id: str,
        session_factory: async_sessionmaker[AsyncSession],
        supervisor: object,
        event_broker: object,
        secret_vault: SecretVault,
    ) -> None:
        self.agent_id = agent_id
        self.session_factory = session_factory
        self.supervisor = supervisor
        self.event_broker = event_broker
        self.secret_vault = secret_vault

        self._running = False
        self._api_key: str | None = None
        self._phone_number_id: str | None = None
        self._webhook_secret: str | None = None
        self._pending_tasks: dict[str, dict] = {}  # task_id → delivery info
        self._pending_tasks_ttl = 1800  # 30 minutes
        self._cleanup_task: asyncio.Task | None = None

    # ---- lifecycle ----

    async def start(self) -> None:
        """Load credentials and validate connectivity."""
        creds = await self._load_credentials()
        if not creds:
            raise ValueError("Kapso WhatsApp credentials not configured")
        self._api_key, self._phone_number_id, self._webhook_secret = creds

        # Validate API key by listing phone numbers (lightweight check)
        await self._validate_connectivity()

        self._running = True
        self._cleanup_task = asyncio.create_task(self._pending_tasks_cleanup_loop())
        self.event_broker.subscribe(self._on_event)
        logger.info(
            "Kapso WhatsApp gateway started for agent %s (phone_number_id=%s)",
            self.agent_id, self._phone_number_id,
        )

    async def stop(self) -> None:
        """Stop and clean up."""
        self._running = False
        self.event_broker.unsubscribe(self._on_event)
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        self._pending_tasks.clear()
        logger.info("Kapso WhatsApp gateway stopped for agent %s", self.agent_id)

    # ---- credential loading ----

    async def _load_credentials(self) -> tuple[str, str, str | None] | None:
        """
        Read Kapso credentials from the messaging channel config.

        Returns (api_key, phone_number_id, webhook_secret) or None.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(MessagingChannel).where(
                    MessagingChannel.agent_id == self.agent_id,
                    MessagingChannel.platform == "kapso_whatsapp",
                )
            )
            channel = result.scalar_one_or_none()
            if not channel or not channel.secret_ref:
                return None

            secret_result = await session.execute(
                select(Secret).where(Secret.name == channel.secret_ref)
            )
            secret = secret_result.scalar_one_or_none()
            if not secret:
                return None

            api_key = self.secret_vault.decrypt(secret.value_enc)
            if not api_key:
                return None

            metadata = channel.metadata_json or {}
            phone_number_id = metadata.get("kapso_phone_number_id", "")
            webhook_secret = metadata.get("kapso_webhook_secret")

            if not phone_number_id:
                logger.error(
                    "Kapso WhatsApp: missing kapso_phone_number_id in channel metadata "
                    "for agent %s", self.agent_id,
                )
                return None

            return api_key, phone_number_id, webhook_secret

    async def _validate_connectivity(self) -> None:
        """Validate that the API key works by checking phone numbers."""
        url = "https://api.kapso.ai/platform/v1/whatsapp/phone_numbers"
        headers = {"X-API-Key": self._api_key}
        try:
            client = _get_http_client()
            resp = await client.get(url, headers=headers, timeout=15)
            if resp.status_code == 401:
                raise ValueError("Kapso API key is invalid (401 Unauthorized)")
            resp.raise_for_status()
            data = resp.json()
            # Kapso v1 wraps results in "data" array
            phone_numbers = data.get("data", data.get("phone_numbers", []))
            found = any(
                pn.get("phone_number_id") == self._phone_number_id
                or pn.get("id") == self._phone_number_id
                for pn in phone_numbers
            )
            if not found:
                logger.warning(
                    "Kapso phone_number_id %s not found in account numbers: %s",
                    self._phone_number_id,
                    [pn.get("id") for pn in phone_numbers],
                )
        except httpx.HTTPStatusError as exc:
            logger.warning("Kapso connectivity check failed: %s", exc)
            raise ValueError(f"Kapso connectivity check failed: {exc}") from exc

    # ---- channel config ----

    async def _load_channel_config(self) -> dict:
        """Load messaging channel configuration."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(MessagingChannel).where(
                    MessagingChannel.agent_id == self.agent_id,
                    MessagingChannel.platform == "kapso_whatsapp",
                )
            )
            channel = result.scalar_one_or_none()
            if not channel:
                return {}
            return {
                "allowed_user_ids": channel.allowed_user_ids or [],
                "home_chat_id": channel.home_chat_id,
                "require_mention": channel.require_mention or False,
                "unauthorized_dm_behavior": channel.unauthorized_dm_behavior or "pair",
                "enabled": channel.enabled,
            }

    # ---- incoming message handling (from webhook) ----

    async def handle_webhook_event(self, event_type: str, payload: dict) -> None:
        """
        Process an incoming Kapso webhook event.

        Called from the webhook router after signature verification.
        """
        if not self._running:
            return

        if event_type == EVENT_MESSAGE_RECEIVED:
            await self._handle_message_received(payload)
        elif event_type == EVENT_MESSAGE_DELIVERED:
            await self._handle_status_update("delivered", payload)
        elif event_type == EVENT_MESSAGE_READ:
            await self._handle_status_update("read", payload)
        elif event_type == EVENT_MESSAGE_FAILED:
            await self._handle_status_update("failed", payload)
        elif event_type == EVENT_CONVERSATION_ENDED:
            logger.info(
                "Kapso conversation ended for agent %s: %s",
                self.agent_id,
                payload.get("conversation", {}).get("id"),
            )
        else:
            logger.debug("Kapso: unhandled event type %s", event_type)

    async def _handle_message_received(self, payload: dict) -> None:
        """Process an incoming WhatsApp message received via Kapso."""
        message = payload.get("message", {})
        conversation = payload.get("conversation", {})

        # Extract message content
        msg_type = message.get("type", "text")
        text_content = ""

        if msg_type == "text":
            text_content = message.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            interactive = message.get("interactive", {})
            if interactive.get("type") == "button_reply":
                text_content = interactive.get("button_reply", {}).get("title", "")
            elif interactive.get("type") == "list_reply":
                text_content = interactive.get("list_reply", {}).get("title", "")
            else:
                text_content = json.dumps(interactive)
        else:
            # For media messages, include caption or type info
            media_data = message.get(msg_type, {})
            caption = media_data.get("caption", "")
            text_content = f"[{msg_type}] {caption}" if caption else f"[{msg_type}]"

        if not text_content.strip():
            return

        # Extract sender info
        sender_wa_id = message.get("from", "")
        sender_username = message.get("username", "")
        message_id = message.get("id", "")
        conversation_id = conversation.get("id", "")

        # Normalize sender phone (remove + prefix for matching)
        sender_phone = sender_wa_id.lstrip("+")

        # --- Access control ---
        config = await self._load_channel_config()
        allowed = config.get("allowed_user_ids", [])
        unauthorized_behavior = config.get("unauthorized_dm_behavior", "pair")

        logger.debug("Kapso access check: sender_wa_id=%s sender_phone=%s allowed=%s",
                     sender_wa_id, sender_phone, allowed)
        if allowed:
            # Check if sender matches any allowed user
            matched = False
            for allowed_id in allowed:
                normalized = allowed_id.lstrip("+").strip()
                if normalized in (sender_phone, sender_wa_id):
                    matched = True
                    break
                # Also check username
                if sender_username and normalized == sender_username.lstrip("@"):
                    matched = True
                    break
            if not matched:
                logger.info(
                    "Kapso WhatsApp: sender %s not in allowed list — %s",
                    sender_wa_id, unauthorized_behavior,
                )
                if unauthorized_behavior == "pair":
                    try:
                        await kapso_send_text(
                            self._api_key,
                            self._phone_number_id,
                            sender_wa_id,
                            "👋 Hi! You are not authorized to use this bot.",
                        )
                    except (httpx.HTTPError, RuntimeError):
                        logger.exception("Failed to send unauthorized reply")
                return

        # Mark message as read and send acknowledgment
        if message_id:
            t1 = asyncio.create_task(
                kapso_mark_read(self._api_key, self._phone_number_id, message_id)
            )
            t1.add_done_callback(lambda t: logger.exception("kapso_mark_read failed", exc_info=t.exception()) if t.exception() else None)
        t2 = asyncio.create_task(
            kapso_send_text(self._api_key, self._phone_number_id, sender_wa_id, "⏳")
        )
        t2.add_done_callback(lambda t: logger.exception("kapso_send_text failed", exc_info=t.exception()) if t.exception() else None)

        # Resolve HermesHQ user from the sender's Kapso ID (kapso_id stores the WA phone number)
        hermeshq_user_id: str | None = None
        if sender_wa_id:
            async with self.session_factory() as session:
                resolved = await resolve_channel_user(session, "kapso_whatsapp", sender_wa_id)
                if resolved:
                    hermeshq_user_id = resolved.id

        # Create task for the agent
        task_id = await self._create_task(
            prompt=text_content,
            sender_wa_id=sender_wa_id,
            sender_username=sender_username,
            message_id=message_id,
            conversation_id=conversation_id,
            msg_type=msg_type,
            hermeshq_user_id=hermeshq_user_id,
        )

        if task_id:
            logger.info(
                "Kapso WhatsApp → agent %s: created task %s from %s",
                self.agent_id, task_id, sender_wa_id,
            )

    async def _create_task(
        self,
        prompt: str,
        sender_wa_id: str,
        sender_username: str,
        message_id: str,
        conversation_id: str,
        msg_type: str,
        hermeshq_user_id: str | None = None,
    ) -> str | None:
        """Create a Task and submit it to the supervisor."""
        task_id = str(uuid.uuid4())
        async with self.session_factory() as session:
            agent = await session.get(Agent, self.agent_id)
            if not agent or agent.status != "running":
                logger.warning(
                    "Agent %s not running (status=%s), skipping task creation",
                    self.agent_id,
                    agent.status if agent else "missing",
                )
                return None

            task = Task(
                id=task_id,
                agent_id=self.agent_id,
                title=f"WhatsApp: {sender_wa_id}",
                prompt=prompt,
                status="queued",
                metadata_json={
                    "source": "kapso_whatsapp",
                    "platform": "kapso_whatsapp",
                    "sender_wa_id": sender_wa_id,
                    "sender_username": sender_username,
                    "kapso_message_id": message_id,
                    "kapso_conversation_id": conversation_id,
                    "msg_type": msg_type,
                    "kapso_phone_number_id": self._phone_number_id,
                    "hermeshq_user_id": hermeshq_user_id,
                    # thread_user_id allows M365 plugins to identify the user
                    # and retrieve their OAuth token for Graph API calls.
                    "thread_user_id": hermeshq_user_id,
                },
            )
            session.add(task)
            await session.commit()

        self._pending_tasks[task_id] = {
            "sender_wa_id": sender_wa_id,
            "message_id": message_id,
            "conversation_id": conversation_id,
            "_created_at": time.monotonic(),
        }
        await self.supervisor.submit_task(task_id)
        return task_id

    async def _handle_status_update(self, status: str, payload: dict) -> None:
        """Handle delivery/read/failed status updates."""
        message = payload.get("message", {})
        message_id = message.get("id", "")
        logger.debug(
            "Kapso WhatsApp: message %s status=%s for agent %s",
            message_id, status, self.agent_id,
        )

    async def _pending_tasks_cleanup_loop(self) -> None:
        """Evict stale entries from _pending_tasks that never received a completion event."""
        while self._running:
            try:
                await asyncio.sleep(300)
                now = time.monotonic()
                stale = [
                    tid for tid, info in self._pending_tasks.items()
                    if now - info.get("_created_at", now) > self._pending_tasks_ttl
                ]
                for tid in stale:
                    self._pending_tasks.pop(tid, None)
                    logger.warning("Evicted stale pending task %s (TTL expired)", tid)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug("pending_tasks cleanup error", exc_info=True)

    # ---- event handler (task completion) ----

    async def _on_event(self, event: dict) -> None:
        """Handle task completion events from the EventBroker."""
        event_type = event.get("type", "")
        if event_type not in ("task.completed", "task.failed"):
            return

        task_id = event.get("task_id")
        if task_id not in self._pending_tasks:
            return

        delivery = self._pending_tasks.pop(task_id, None)
        if not delivery:
            return

        if event_type == "task.completed":
            response_text = event.get("response", "")
            if not response_text:
                return
            try:
                await kapso_send_text_chunked(
                    api_key=self._api_key,
                    phone_number_id=self._phone_number_id,
                    to=delivery["sender_wa_id"],
                    text=response_text,
                )
                logger.info("Kapso WhatsApp reply sent for task %s", task_id)
            except (httpx.HTTPError, RuntimeError):
                logger.exception(
                    "Failed to send Kapso WhatsApp reply for task %s", task_id
                )
        else:
            logger.warning("Task %s failed, not sending WhatsApp reply", task_id)


# ---------------------------------------------------------------------------
# Webhook handler for Kapso events
# ---------------------------------------------------------------------------


async def handle_kapso_webhook(
    event_type: str,
    payload: dict,
    gateways: dict[str, KapsoWhatsAppGateway],
    webhook_secret: str | None = None,
) -> None:
    """
    Route incoming Kapso webhook events to the appropriate gateway.

    Matches gateway by phone_number_id present in the payload.
    """
    phone_number_id = payload.get("phone_number_id", "")

    if not phone_number_id:
        # Try to extract from conversation data
        conversation = payload.get("conversation", {})
        phone_number_id = conversation.get("phone_number_id", "")

    if not phone_number_id:
        logger.warning("Kapso webhook: no phone_number_id in payload — full payload keys: %s", list(payload.keys()))
        return

    logger.debug("Kapso webhook: phone_number_id=%s, registered gateways: %s",
                 phone_number_id,
                 {aid: gw._phone_number_id for aid, gw in gateways.items()})

    # Find the gateway for this phone number
    for agent_id, gateway in gateways.items():
        if not gateway._running:
            continue
        if gateway._phone_number_id == phone_number_id:
            try:
                await gateway.handle_webhook_event(event_type, payload)
            except Exception:  # noqa: BLE001  # gateway webhook handler — catch all
                logger.exception(
                    "Kapso gateway error for agent %s", agent_id
                )
            return

    logger.warning(
        "Kapso webhook: no gateway found for phone_number_id=%s",
        phone_number_id,
    )
