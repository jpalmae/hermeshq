from collections.abc import Callable, Iterable
from dataclasses import dataclass
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class EventSubscription:
    websocket: WebSocket
    is_admin: bool
    agent_ids: set[str]


class EventBroker:
    def __init__(self) -> None:
        self._connections: dict[WebSocket, EventSubscription] = {}
        self._internal_subscribers: list[Callable] = []

    async def connect(self, websocket: WebSocket, is_admin: bool, agent_ids: set[str]) -> None:
        await websocket.accept()
        self._connections[websocket] = EventSubscription(
            websocket=websocket,
            is_admin=is_admin,
            agent_ids=set(agent_ids),
        )

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    def subscribe(self, callback: Callable) -> None:
        """Register an internal async callback to receive all published events."""
        if callback not in self._internal_subscribers:
            self._internal_subscribers.append(callback)

    def unsubscribe(self, callback: Callable) -> None:
        """Remove a previously registered internal callback."""
        try:
            self._internal_subscribers.remove(callback)
        except ValueError:
            pass

    async def publish(self, event: dict) -> None:
        # Notify internal subscribers first (gateways, services, etc.)
        for callback in list(self._internal_subscribers):
            try:
                await callback(event)
            except Exception:
                logger.exception("Internal subscriber %s failed", getattr(callback, "__qualname__", callback))

        # Then push to WebSocket connections (frontend)
        stale_connections: list[WebSocket] = []
        event_agent_id = event.get("agent_id")
        for connection, subscription in list(self._connections.items()):
            if event_agent_id and not subscription.is_admin and event_agent_id not in subscription.agent_ids:
                continue
            try:
                await connection.send_json(event)
            except Exception:
                stale_connections.append(connection)
        for connection in stale_connections:
            self.disconnect(connection)

    async def publish_many(self, events: Iterable[dict]) -> None:
        for event in events:
            await self.publish(event)
