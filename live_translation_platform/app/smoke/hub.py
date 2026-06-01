from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class SmokeEventHub:
    def __init__(self) -> None:
        self._clients: dict[str, set[Any]] = defaultdict(set)

    async def connect(self, smoke_test_id: str, websocket: WebSocket) -> None:
        self._clients[smoke_test_id].add(websocket)

    def disconnect(self, smoke_test_id: str, websocket: WebSocket) -> None:
        self._clients[smoke_test_id].discard(websocket)
        if not self._clients[smoke_test_id]:
            self._clients.pop(smoke_test_id, None)

    async def broadcast(self, smoke_test_id: str, payload: dict) -> None:
        stale = []
        for websocket in list(self._clients.get(smoke_test_id, set())):
            try:
                await websocket.send_json(payload)
            except RuntimeError:
                stale.append(websocket)
        for websocket in stale:
            self._clients[smoke_test_id].discard(websocket)
