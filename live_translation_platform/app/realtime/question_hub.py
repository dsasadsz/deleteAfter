import asyncio
from collections import defaultdict
from time import perf_counter
from typing import Any

from fastapi import WebSocket


class QuestionHub:
    def __init__(
        self,
        pubsub=None,
        runtime_metrics=None,
        *,
        send_timeout_seconds: float = 2.0,
        max_concurrency: int = 100,
        drop_on_timeout: bool = True,
        metrics_enabled: bool = True,
    ) -> None:
        self._clients: dict[str, set[Any]] = defaultdict(set)
        self.pubsub = pubsub
        self.runtime_metrics = runtime_metrics
        self.send_timeout_seconds = max(0.001, float(send_timeout_seconds or 2.0))
        self.max_concurrency = max(1, int(max_concurrency or 100))
        self.drop_on_timeout = bool(drop_on_timeout)
        self.metrics_enabled = bool(metrics_enabled)

    def attach_pubsub(self, pubsub) -> None:
        self.pubsub = pubsub

    async def connect(self, lesson_id: str, websocket: WebSocket) -> None:
        self._clients[lesson_id].add(websocket)

    def disconnect(self, lesson_id: str, websocket: WebSocket) -> None:
        self._clients[lesson_id].discard(websocket)
        if not self._clients[lesson_id]:
            self._clients.pop(lesson_id, None)

    async def broadcast(self, lesson_id: str, payload: dict) -> None:
        if self.pubsub is not None and await self.pubsub.publish_question(lesson_id, payload):
            return
        await self.deliver(lesson_id, payload)

    async def deliver(self, lesson_id: str, payload: dict) -> None:
        if self.runtime_metrics is not None and payload.get("event") == "question_created":
            self.runtime_metrics.record_question()
        await self._broadcast(lesson_id, payload)

    def connected_count(self, lesson_id: str) -> int:
        return len(self._clients.get(lesson_id, set()))

    async def _broadcast(self, lesson_id: str, payload: dict) -> None:
        started_at = perf_counter()
        websockets = list(self._clients.get(lesson_id, set()))
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def send_one(websocket):
            async with semaphore:
                try:
                    await asyncio.wait_for(websocket.send_json(payload), timeout=self.send_timeout_seconds)
                    return None
                except asyncio.TimeoutError:
                    self._record_send_timeout()
                    return websocket if self.drop_on_timeout else None
                except Exception:
                    self._record_send_failure()
                    return websocket

        stale = [websocket for websocket in await asyncio.gather(*(send_one(websocket) for websocket in websockets)) if websocket is not None]
        if stale:
            lesson_clients = self._clients.get(lesson_id)
            if lesson_clients is not None:
                for websocket in stale:
                    lesson_clients.discard(websocket)
                if not lesson_clients:
                    self._clients.pop(lesson_id, None)
            self._record_clients_dropped(len(stale))
        self._record_broadcast((perf_counter() - started_at) * 1000)

    def _record_broadcast(self, latency_ms: float) -> None:
        if self.metrics_enabled and self.runtime_metrics is not None:
            self.runtime_metrics.record_websocket_broadcast("question", latency_ms)

    def _record_send_failure(self) -> None:
        if self.metrics_enabled and self.runtime_metrics is not None:
            self.runtime_metrics.record_websocket_send_failure()

    def _record_send_timeout(self) -> None:
        if self.metrics_enabled and self.runtime_metrics is not None:
            self.runtime_metrics.record_websocket_send_timeout()

    def _record_clients_dropped(self, count: int) -> None:
        if self.metrics_enabled and self.runtime_metrics is not None:
            self.runtime_metrics.record_websocket_client_dropped(count)
