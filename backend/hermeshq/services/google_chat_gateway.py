"""
Google Chat Gateway — Google Chat API integration.

Receives messages via webhook events and forwards them as tasks
to the agent supervisor.  When a task completes, the response
is posted back to the originating Google Chat space.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.services.channel_user_resolver import resolve_channel_user

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

from hermeshq.models.agent import Agent
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.secret import Secret
from hermeshq.models.task import Task
from hermeshq.services.secret_vault import SecretVault

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google Auth helpers
# ---------------------------------------------------------------------------

_CHAT_API_BASE = "https://chat.googleapis.com/v1"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/chat.bot"


async def _get_service_account_token(service_account_json: str) -> str:
    """
    Obtain an access token using a Google service account JSON key.

    Uses the JWT grant flow (RFC 7523) for server-to-server auth.
    """
    import base64
    import time

    sa = json.loads(service_account_json)
    client_email = sa["client_email"]
    private_key = sa["private_key"]
    token_uri = sa.get("token_uri", _TOKEN_URL)

    # Build JWT
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": client_email,
        "scope": _SCOPE,
        "aud": token_uri,
        "iat": now,
        "exp": now + 3600,
    }

    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    header_b64 = _b64(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sign_input = f"{header_b64}.{payload_b64}".encode()

    private_key_obj = serialization.load_pem_private_key(
        private_key.encode(), password=None, backend=default_backend()
    )
    signature = private_key_obj.sign(sign_input, padding.PKCS1v15(), hashes.SHA256())
    jwt_token = f"{header_b64}.{payload_b64}.{_b64(signature)}"

    client = _get_http_client()
    resp = await client.post(
        token_uri,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt_token,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Google Chat API helpers
# ---------------------------------------------------------------------------


async def _send_message(
    token: str,
    space_name: str,
    text: str,
    thread_name: str | None = None,
) -> dict:
    """Send a message to a Google Chat space."""
    url = f"{_CHAT_API_BASE}/{space_name}/messages"
    body: dict = {"text": text}
    if thread_name:
        body["thread"] = {"name": thread_name}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    client = _get_http_client()
    resp = await client.post(url, json=body, headers=headers)
    resp.raise_for_status()
    return resp.json()


async def _get_space(
    token: str,
    space_name: str,
) -> dict:
    """Get space info from Google Chat."""
    url = f"{_CHAT_API_BASE}/{space_name}"
    headers = {"Authorization": f"Bearer {token}"}
    client = _get_http_client()
    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Google Chat Gateway
# ---------------------------------------------------------------------------


class GoogleChatGateway:
    """
    Manages the lifecycle of a Google Chat bot connection for a single
    HermesHQ agent.

    Google Chat uses a webhook model — Google sends events to our
    endpoint, and we send messages back via the REST API.
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
        self._token: str | None = None
        self._token_refresh_task: asyncio.Task | None = None
        self._service_account_json: str = ""
        self._project_id: str = ""
        self._pending_tasks: dict[str, dict] = {}  # task_id → delivery info
        self._pending_tasks_ttl = 1800  # 30 minutes
        self._cleanup_task: asyncio.Task | None = None
        self._known_spaces: set[str] = set()  # spaces this gateway has been added to

    # ---- lifecycle ----

    async def start(self) -> None:
        """Load credentials, obtain token."""
        creds = await self._load_credentials()
        if not creds:
            raise ValueError("Google Chat credentials not configured")
        self._service_account_json, self._project_id = creds
        self._token = await _get_service_account_token(self._service_account_json)
        self._running = True
        self._token_refresh_task = asyncio.create_task(self._token_refresh_loop())
        self._cleanup_task = asyncio.create_task(self._pending_tasks_cleanup_loop())
        # Subscribe to task completion events
        self.event_broker.subscribe(self._on_event)
        logger.info("Google Chat gateway started for agent %s", self.agent_id)

    async def stop(self) -> None:
        """Stop and clean up."""
        self._running = False
        self.event_broker.unsubscribe(self._on_event)
        for task in (self._token_refresh_task, self._cleanup_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._pending_tasks.clear()
        logger.info("Google Chat gateway stopped for agent %s", self.agent_id)

    # ---- credential loading ----

    async def _load_credentials(self) -> tuple[str, str] | None:
        """Read Google Chat credentials from the messaging channel config."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(MessagingChannel).where(
                    MessagingChannel.agent_id == self.agent_id,
                    MessagingChannel.platform == "google_chat",
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

            metadata = channel.metadata_json or {}
            # The service account JSON is stored as the encrypted secret value
            service_account_json = self.secret_vault.decrypt(secret.value_enc)
            project_id = metadata.get("project_id", "")

            if not service_account_json:
                return None
            return service_account_json, project_id

    # ---- token refresh ----

    async def _token_refresh_loop(self) -> None:
        """Refresh the Google access token every 45 minutes (tokens last ~1h)."""
        while self._running:
            try:
                await asyncio.sleep(2700)  # 45 minutes
                self._token = await _get_service_account_token(self._service_account_json)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to refresh Google Chat token for agent %s", self.agent_id
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

    # ---- incoming message handling (from webhook) ----

    async def _load_channel_config(self) -> dict:
        """Load the messaging channel configuration (allowed users, mention gating, etc)."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(MessagingChannel).where(
                    MessagingChannel.agent_id == self.agent_id,
                    MessagingChannel.platform == "google_chat",
                )
            )
            channel = result.scalar_one_or_none()
            if not channel:
                return {}
            return {
                "allowed_user_ids": channel.allowed_user_ids or [],
                "require_mention": channel.require_mention or False,
                "unauthorized_dm_behavior": channel.unauthorized_dm_behavior or "pair",
            }

    async def handle_event(self, event: dict) -> dict | None:
        """
        Process an incoming Google Chat event (called from webhook router).

        Google Chat sends events for:
        - MESSAGE: new message from user
        - ADDED_TO_SPACE: bot was added to a space
        - REMOVED_FROM_SPACE: bot was removed
        - CARD_CLICKED: interactive card click
        """
        event_type = event.get("type", "")

        if event_type == "ADDED_TO_SPACE":
            space = event.get("space", {})
            sname = space.get("name", "")
            if sname:
                self._known_spaces.add(sname)
            logger.info(
                "Google Chat bot added to space %s (%s) for agent %s",
                sname, space.get("displayName"), self.agent_id,
            )
            return {"text": "Hello! I'm the HermesHQ agent bot. Send me a message to get started."}

        if event_type == "REMOVED_FROM_SPACE":
            space = event.get("space", {})
            sname = space.get("name", "")
            self._known_spaces.discard(sname)
            return None

        if event_type == "CARD_CLICKED":
            # Handle card interactions if needed
            action = event.get("action", {})
            _action_name = action.get("actionMethodName", "")  # noqa: F841
            return None

        if event_type != "MESSAGE":
            return None

        # Process message
        message = event.get("message", {})
        # argumentText already has @mention stripped by Google Chat
        text = (message.get("argumentText") or message.get("text") or "").strip()
        if not text:
            return None

        sender = message.get("sender", {})
        sender_name = sender.get("displayName", "Google Chat User")
        sender_email = sender.get("email", "")

        space = event.get("space", {})
        space_name = space.get("name", "")
        space_type = space.get("spaceType", "")

        # In multi-agent setups each gateway tracks its own spaces. If this
        # gateway has known spaces and this message is from a different space,
        # let another gateway handle it.
        if self._known_spaces and space_name not in self._known_spaces:
            return None
        thread_name = message.get("thread", {}).get("name")
        message_name = message.get("name", "")

        # --- Access control ---
        config = await self._load_channel_config()
        allowed = config.get("allowed_user_ids", [])
        require_mention = config.get("require_mention", False)
        unauthorized_behavior = config.get("unauthorized_dm_behavior", "pair")

        is_dm = space_type == "DIRECT_MESSAGE"

        # Check allowed users (matched by email)
        if allowed and sender_email and sender_email not in allowed:
            logger.info(
                "Google Chat: sender %s (%s) not in allowed list — ignoring",
                sender_name, sender_email,
            )
            if unauthorized_behavior == "pair":
                return {"text": f"👋 Hi {sender_name}, you are not authorized to use this bot."}
            return None

        # Check require_mention in group spaces (DMs always pass)
        if require_mention and not is_dm:
            # Google Chat only sends events when @mentioned if configured in
            # the Google Cloud console.  If we receive it here and require_mention
            # is on, we still check for annotation-based mentions.
            annotations = message.get("annotations", [])
            has_user_mention = any(
                a.get("type") == "USER_MENTION"
                for a in annotations
            )
            if not has_user_mention:
                return None

        # Resolve HermesHQ user from the sender's Google Chat email
        hermeshq_user_id: str | None = None
        if sender_email:
            async with self.session_factory() as session:
                resolved = await resolve_channel_user(session, "google_chat", sender_email)
                if resolved:
                    hermeshq_user_id = resolved.id

        # Create task for the agent
        task_id = await self._create_task(
            prompt=text,
            sender_name=sender_name,
            sender_email=sender_email,
            space_name=space_name,
            thread_name=thread_name,
            message_name=message_name,
            hermeshq_user_id=hermeshq_user_id,
        )

        if task_id:
            logger.info(
                "Google Chat → agent %s: created task %s from %s",
                self.agent_id, task_id, sender_name,
            )
            return {"text": "⏳ Processing your message..."}

        return None

    async def _create_task(
        self,
        prompt: str,
        sender_name: str,
        sender_email: str,
        space_name: str,
        thread_name: str | None,
        message_name: str,
        hermeshq_user_id: str | None = None,
    ) -> str | None:
        """Create a Task and submit it to the supervisor."""
        task_id = str(uuid.uuid4())
        async with self.session_factory() as session:
            agent = await session.get(Agent, self.agent_id)
            if not agent or agent.status != "running":
                return None

            task = Task(
                id=task_id,
                agent_id=self.agent_id,
                title=f"GChat: {sender_name}",
                prompt=prompt,
                status="queued",
                metadata_json={
                    "source": "google_chat",
                    "sender_name": sender_name,
                    "sender_email": sender_email,
                    "gchat_space_name": space_name,
                    "gchat_thread_name": thread_name,
                    "gchat_message_name": message_name,
                    "hermeshq_user_id": hermeshq_user_id,
                },
            )
            session.add(task)
            await session.commit()

        self._pending_tasks[task_id] = {
            "space_name": space_name,
            "thread_name": thread_name,
            "_created_at": time.monotonic(),
        }
        await self.supervisor.submit_task(task_id)
        return task_id

    # ---- event handler (task completion) ----

    async def _on_event(self, event: dict) -> None:
        """Handle task completion/failure events from the EventBroker."""
        event_type = event.get("type", "")
        if event_type not in ("task.completed", "task.failed"):
            return
        task_id = event.get("task_id")
        if task_id not in self._pending_tasks:
            return

        delivery = self._pending_tasks.pop(task_id, None)
        if not delivery:
            return

        if event_type == "task.failed":
            logger.warning("Task %s failed, sending error reply to Google Chat", task_id)
            response_text = "Sorry, an error occurred while processing your message."
        else:
            response_text = event.get("response", "")
            if not response_text:
                return

        try:
            token = self._token or await _get_service_account_token(
                self._service_account_json
            )
            await _send_message(
                token=token,
                space_name=delivery["space_name"],
                text=response_text,
                thread_name=delivery.get("thread_name"),
            )
            logger.info("Google Chat reply sent for task %s", task_id)
        except httpx.HTTPError:
            logger.exception("Failed to send Google Chat reply for task %s", task_id)



# ---------------------------------------------------------------------------
# Webhook handler for Google Chat events
# ---------------------------------------------------------------------------


async def handle_google_chat_webhook(
    payload: dict,
    session_factory: async_sessionmaker[AsyncSession],
    gateways: dict[str, "GoogleChatGateway"],
) -> dict | None:
    """
    Route incoming Google Chat webhook events to the appropriate gateway.

    Google Chat sends all events to a single webhook URL. We route
    based on the bot's project_id or space membership.
    """
    # Try all gateways — first one that matches the space handles it
    for agent_id, gateway in gateways.items():
        if not gateway._running:
            continue
        try:
            result = await gateway.handle_event(payload)
            if result is not None:
                return result
        except Exception:  # noqa: BLE001
            logger.exception("Google Chat gateway error for agent %s", agent_id)
    return None
