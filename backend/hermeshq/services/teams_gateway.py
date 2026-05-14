"""
Microsoft Teams Gateway — Bot Framework SDK integration.

Subscribes to Teams messages via Bot Framework WebSocket and
forwards them as tasks to the agent supervisor.  When a task
completes, the response is posted back to the originating
Teams conversation.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hermeshq.models.agent import Agent
from hermeshq.models.base import utcnow
from hermeshq.models.messaging_channel import MessagingChannel
from hermeshq.models.secret import Secret
from hermeshq.models.task import Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bot Framework OAuth
# ---------------------------------------------------------------------------

_TENANTLESS = "botframework.com"
_TOKEN_URL_FMT = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
)
_SCOPE = "https://api.botframework.com/.default"


async def _get_app_token(
    app_id: str, app_password: str, tenant_id: str | None = None
) -> str:
    """Exchange app credentials for a Bot Framework access token."""
    tenant = tenant_id or _TENANTLESS
    url = _TOKEN_URL_FMT.format(tenant=tenant)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            data={
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_password,
                "scope": _SCOPE,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Teams API helpers
# ---------------------------------------------------------------------------

_BOTFRAMEWORK_BASE = "https://smba.trafficmanager.net/amer"


async def _reply_to_activity(
    token: str,
    service_url: str,
    conversation_id: str,
    activity_id: str,
    text: str,
) -> None:
    """Send a reply to a Teams conversation activity."""
    url = (
        f"{service_url}/v3/conversations/{conversation_id}"
        f"/activities/{activity_id}"
    )
    body = {
        "type": "message",
        "text": text,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()


async def _send_proactive_message(
    token: str,
    service_url: str,
    conversation_id: str,
    text: str,
) -> None:
    """Send a proactive message to a conversation."""
    url = f"{service_url}/v3/conversations/{conversation_id}/activities"
    body = {
        "type": "message",
        "text": text,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Teams Gateway
# ---------------------------------------------------------------------------


class TeamsGateway:
    """
    Manages the lifecycle of a Microsoft Teams bot connection for a single
    HermesHQ agent.

    Flow:
    1. start() — authenticates, opens polling loop for activities
    2. Incoming messages → create Task → submit to supervisor
    3. Subscribe to task completion events → post response back to Teams
    """

    def __init__(
        self,
        agent_id: str,
        session_factory: async_sessionmaker[AsyncSession],
        supervisor: object,
        event_broker: object,
    ) -> None:
        self.agent_id = agent_id
        self.session_factory = session_factory
        self.supervisor = supervisor
        self.event_broker = event_broker

        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._token_refresh_task: asyncio.Task | None = None
        self._token: str | None = None
        self._watermark: str | None = None
        self._pending_tasks: dict[str, dict] = {}  # task_id → delivery info
        self._app_id: str = ""
        self._app_password: str = ""
        self._tenant_id: str | None = None
        self._conversation_refs: dict[str, dict] = {}  # conv_id → service_url

    # ---- lifecycle ----

    async def start(self) -> None:
        """Load credentials, obtain token, start polling."""
        creds = await self._load_credentials()
        if not creds:
            raise ValueError("Teams bot credentials not configured")
        self._app_id, self._app_password, self._tenant_id = creds
        self._token = await _get_app_token(self._app_id, self._app_password, self._tenant_id)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._token_refresh_task = asyncio.create_task(self._token_refresh_loop())
        # Subscribe to task completion events
        self.event_broker.subscribe(self._on_event)
        logger.info("Teams gateway started for agent %s", self.agent_id)

    async def stop(self) -> None:
        """Stop polling and clean up."""
        self._running = False
        self.event_broker.unsubscribe(self._on_event)
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        if self._token_refresh_task:
            self._token_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._token_refresh_task
        logger.info("Teams gateway stopped for agent %s", self.agent_id)

    # ---- credential loading ----

    async def _load_credentials(self) -> tuple[str, str, str | None] | None:
        """Read Teams bot credentials from the messaging channel config."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(MessagingChannel).where(
                    MessagingChannel.agent_id == self.agent_id,
                    MessagingChannel.platform == "microsoft_teams",
                )
            )
            channel = result.scalar_one_or_none()
            if not channel or not channel.secret_ref:
                return None

            # secret_ref points to a Secret with bot app_id in username and password in value
            secret_result = await session.execute(
                select(Secret).where(Secret.name == channel.secret_ref)
            )
            secret = secret_result.scalar_one_or_none()
            if not secret:
                return None

            metadata = channel.metadata_json or {}
            app_id = metadata.get("app_id", secret.username or "")
            app_password = secret.value
            tenant_id = metadata.get("tenant_id")

            if not app_id or not app_password:
                return None
            return app_id, app_password, tenant_id

    # ---- token refresh ----

    async def _token_refresh_loop(self) -> None:
        """Refresh the Bot Framework token every 5 minutes (tokens last ~1h)."""
        while self._running:
            try:
                await asyncio.sleep(300)  # 5 minutes
                self._token = await _get_app_token(
                    self._app_id, self._app_password, self._tenant_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to refresh Teams token for agent %s", self.agent_id)

    # ---- message polling ----

    async def _poll_loop(self) -> None:
        """
        Poll Teams for new activities using the Bot Framework REST API.

        We use long-polling via the conversations/activities endpoint.
        """
        while self._running:
            try:
                await self._poll_activities()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Teams poll error for agent %s", self.agent_id)
                await asyncio.sleep(5)

    async def _poll_activities(self) -> None:
        """Fetch new activities from the Bot Framework."""
        if not self._conversation_refs:
            # No conversations seen yet — wait for webhook or initial handshake
            await asyncio.sleep(2)
            return

        for conv_id, ref in list(self._conversation_refs.items()):
            service_url = ref.get("service_url", "")
            if not service_url or not self._token:
                continue
            url = f"{service_url}/v3/conversations/{conv_id}/activities"
            if self._watermark:
                url += f"?watermark={self._watermark}"
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            }
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    activities = data.get("activities", [])
                    self._watermark = data.get("watermark", self._watermark)
                    for activity in activities:
                        await self._handle_activity(activity, service_url)
            except Exception:
                logger.exception("Error polling Teams activities for conv %s", conv_id)

    # ---- activity handling ----

    async def _handle_activity(self, activity: dict, service_url: str) -> None:
        """Process a single Teams activity."""
        if activity.get("type") != "message":
            return

        # Skip messages from the bot itself
        if activity.get("from", {}).get("id") == self._app_id:
            return

        text = (activity.get("text") or "").strip()
        if not text:
            return

        conversation = activity.get("conversation", {})
        conv_id = conversation.get("id", "")
        sender_name = activity.get("from", {}).get("name", "Teams User")
        activity_id = activity.get("id", "")

        # Store conversation reference for replies
        self._conversation_refs[conv_id] = {
            "service_url": service_url,
            "conversation_id": conv_id,
            "activity_id": activity_id,
        }

        # Create a task for the agent
        task_id = await self._create_task(text, sender_name, conv_id, activity_id, service_url)
        if task_id:
            logger.info(
                "Teams → agent %s: created task %s from %s",
                self.agent_id, task_id, sender_name,
            )

    async def _create_task(
        self,
        prompt: str,
        sender_name: str,
        conv_id: str,
        activity_id: str,
        service_url: str,
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
                title=f"Teams: {sender_name}",
                prompt=prompt,
                status="queued",
                metadata_json={
                    "source": "microsoft_teams",
                    "sender_name": sender_name,
                    "teams_conversation_id": conv_id,
                    "teams_activity_id": activity_id,
                    "teams_service_url": service_url,
                },
            )
            session.add(task)
            await session.commit()

        self._pending_tasks[task_id] = {
            "conv_id": conv_id,
            "activity_id": activity_id,
            "service_url": service_url,
        }
        await self.supervisor.submit_task(task_id)
        return task_id

    # ---- event handler (task completion) ----

    async def _on_event(self, event: dict) -> None:
        """Handle task completion events from the EventBroker."""
        if event.get("type") != "task.completed":
            return
        task_id = event.get("task_id")
        if task_id not in self._pending_tasks:
            return

        response_text = event.get("response", "")
        delivery = self._pending_tasks.pop(task_id, None)
        if not delivery or not response_text:
            return

        try:
            await _reply_to_activity(
                token=self._token or await _get_app_token(
                    self._app_id, self._app_password, self._tenant_id
                ),
                service_url=delivery["service_url"],
                conversation_id=delivery["conv_id"],
                activity_id=delivery["activity_id"],
                text=response_text,
            )
            logger.info("Teams reply sent for task %s", task_id)
        except Exception:
            logger.exception("Failed to send Teams reply for task %s", task_id)


# ---------------------------------------------------------------------------
# Webhook handler for Teams Bot Framework
# ---------------------------------------------------------------------------


async def handle_teams_webhook(
    payload: dict,
    session_factory: async_sessionmaker[AsyncSession],
    gateways: dict[str, "TeamsGateway"],
) -> dict | None:
    """
    Process an incoming Teams webhook payload.

    This is called from a router endpoint when Teams sends
    events to our webhook URL.
    """
    activity = payload

    # Handle verification (Microsoft Bot Framework verification)
    if activity.get("type") == "conversationUpdate":
        return {"status": "ok"}

    if activity.get("type") == "invoke":
        # Handle composeExtension/query etc.
        return {"status": "ok"}

    # Find the right gateway for this agent
    # We use the recipient.id (bot app ID) to route to the correct agent
    recipient = activity.get("recipient", {})
    bot_app_id = recipient.get("id", "")

    for agent_id, gateway in gateways.items():
        if gateway._app_id == bot_app_id:
            service_url = activity.get("serviceUrl", "")
            conv = activity.get("conversation", {})
            conv_id = conv.get("id", "")
            if conv_id and service_url:
                gateway._conversation_refs[conv_id] = {
                    "service_url": service_url,
                    "conversation_id": conv_id,
                    "activity_id": activity.get("id", ""),
                }
            await gateway._handle_activity(activity, service_url)
            return None

    logger.warning("No Teams gateway found for bot app ID %s", bot_app_id)
    return None


import contextlib  # noqa: E402 — needed for stop() suppress
