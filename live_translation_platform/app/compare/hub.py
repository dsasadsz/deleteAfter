from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class ComparisonEventHub:
    def __init__(self) -> None:
        self._clients: dict[str, set[Any]] = defaultdict(set)

    async def connect(self, comparison_id: str, websocket: WebSocket) -> None:
        self._clients[comparison_id].add(websocket)

    def disconnect(self, comparison_id: str, websocket: WebSocket) -> None:
        self._clients[comparison_id].discard(websocket)
        if not self._clients[comparison_id]:
            self._clients.pop(comparison_id, None)

    async def broadcast(self, comparison_id: str, payload: dict) -> None:
        stale = []
        for websocket in list(self._clients.get(comparison_id, set())):
            try:
                await websocket.send_json(payload)
            except RuntimeError:
                stale.append(websocket)
        for websocket in stale:
            self._clients[comparison_id].discard(websocket)
