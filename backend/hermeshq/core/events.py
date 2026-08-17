from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from fastapi import WebSocket

logger = logging.getLogger(__name__)

_WS_SEND_TIMEOUT = 5.0
_INTERNAL_SUBSCRIBER_TIMEOUT = 10.0


@dataclass(frozen=True)
class EventAudience:
    agent_ids: frozenset[str] = field(default_factory=frozenset)
    user_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.agent_ids and not self.user_ids:
            raise ValueError("An event audience must contain at least one agent or user")

    @classmethod
    def for_agent(cls, agent_id: str, *, user_id: str | None = None) -> EventAudience:
        user_ids = frozenset({user_id}) if user_id else frozenset()
        return cls(agent_ids=frozenset({agent_id}), user_ids=user_ids)

    @classmethod
    def for_agents(cls, *agent_ids: str) -> EventAudience:
        return cls(agent_ids=frozenset(agent_ids))

    @classmethod
    def for_user(cls, user_id: str) -> EventAudience:
        return cls(user_ids=frozenset({user_id}))


@dataclass
class EventSubscription:
    websocket: WebSocket
    is_admin: bool
    agent_ids: set[str]
    user_id: str | None = None


class EventBroker:
    def __init__(self) -> None:
        self._connections: dict[WebSocket, EventSubscription] = {}
        self._internal_subscribers: list[Callable] = []

    async def connect(
        self,
        websocket: WebSocket,
        is_admin: bool,
        agent_ids: set[str],
        user_id: str | None = None,
    ) -> None:
        await websocket.accept()
        self.register(websocket, is_admin=is_admin, agent_ids=agent_ids, user_id=user_id)

    def register(
        self,
        websocket: WebSocket,
        is_admin: bool,
        agent_ids: set[str],
        user_id: str | None = None,
    ) -> None:
        """Register an already-accepted WebSocket connection."""
        self._connections[websocket] = EventSubscription(
            websocket=websocket,
            is_admin=is_admin,
            agent_ids=set(agent_ids),
            user_id=user_id,
        )

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    def subscribe(self, callback: Callable) -> None:
        """Register an internal async callback to receive all published events."""
        if callback not in self._internal_subscribers:
            self._internal_subscribers.append(callback)

    def unsubscribe(self, callback: Callable) -> None:
        """Remove a previously registered internal callback."""
        with contextlib.suppress(ValueError):
            self._internal_subscribers.remove(callback)

    async def publish(self, event: dict, *, audience: EventAudience | None = None) -> None:
        # Notify internal subscribers first (gateways, services, etc.)
        snapshot = list(self._internal_subscribers)
        internal_tasks = [self._call_internal(callback, event) for callback in snapshot]
        results = await asyncio.gather(*internal_tasks, return_exceptions=True)
        for callback, result in zip(snapshot, results, strict=False):
            if isinstance(result, Exception):
                logger.exception("Internal subscriber %s failed", getattr(callback, "__qualname__", callback))

        # Then push to WebSocket connections (frontend). Each send has its
        # own timeout so a slow/dead client cannot stall delivery to the
        # rest of subscribers or block the publisher.
        stale_connections: list[WebSocket] = []
        send_tasks: list[tuple[WebSocket, asyncio.Task]] = []
        for connection, subscription in list(self._connections.items()):
            if not subscription.is_admin and not self._matches_audience(subscription, audience):
                continue
            send_tasks.append((connection, asyncio.ensure_future(self._send_with_timeout(connection, event))))

        for connection, task in send_tasks:
            try:
                delivered = await task
                if not delivered:
                    stale_connections.append(connection)
            except Exception:  # noqa: BLE001  # WebSocket send — connection is stale
                stale_connections.append(connection)
        for connection in stale_connections:
            self.disconnect(connection)

    async def _call_internal(self, callback: Callable, event: dict) -> None:
        await asyncio.wait_for(callback(event), timeout=_INTERNAL_SUBSCRIBER_TIMEOUT)

    @staticmethod
    async def _send_with_timeout(connection: WebSocket, event: dict) -> bool:
        try:
            await asyncio.wait_for(connection.send_json(event), timeout=_WS_SEND_TIMEOUT)
            return True
        except TimeoutError:
            logger.warning("Dropping slow WebSocket subscriber (send timeout)")
            return False

    @staticmethod
    def _matches_audience(subscription: EventSubscription, audience: EventAudience | None) -> bool:
        if audience is None:
            return False
        if audience.agent_ids and subscription.agent_ids.isdisjoint(audience.agent_ids):
            return False
        return not audience.user_ids or subscription.user_id in audience.user_ids

    async def publish_many(self, events: Iterable[dict], *, audience: EventAudience | None = None) -> None:
        for event in events:
            await self.publish(event, audience=audience)
