from collections.abc import Iterable

from fastapi import WebSocket


class EventBroker:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def publish(self, event: dict) -> None:
        stale_connections: list[WebSocket] = []
        for connection in list(self._connections):
            try:
                await connection.send_json(event)
            except Exception:
                stale_connections.append(connection)
        for connection in stale_connections:
            self.disconnect(connection)

    async def publish_many(self, events: Iterable[dict]) -> None:
        for event in events:
            await self.publish(event)

