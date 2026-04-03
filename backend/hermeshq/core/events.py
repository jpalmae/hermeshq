from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import WebSocket


@dataclass
class EventSubscription:
    websocket: WebSocket
    is_admin: bool
    agent_ids: set[str]


class EventBroker:
    def __init__(self) -> None:
        self._connections: dict[WebSocket, EventSubscription] = {}

    async def connect(self, websocket: WebSocket, is_admin: bool, agent_ids: set[str]) -> None:
        await websocket.accept()
        self._connections[websocket] = EventSubscription(
            websocket=websocket,
            is_admin=is_admin,
            agent_ids=set(agent_ids),
        )

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    async def publish(self, event: dict) -> None:
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
