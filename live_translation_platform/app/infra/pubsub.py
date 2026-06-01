from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.infra.redis import build_redis_key, sanitize_redis_error

PubSubHandler = Callable[[str, dict], Awaitable[None]]
logger = logging.getLogger(__name__)


@dataclass
class PubSubStatus:
    enabled: bool
    connected: bool
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class RedisPubSubFanout:
    CHANNEL_TYPES = {"captions", "debug", "questions", "diagnostics"}

    def __init__(
        self,
        settings: Any,
        redis_client: Any,
        origin_worker_id: str | None = None,
        runtime_metrics: Any | None = None,
    ) -> None:
        self.settings = settings
        self.redis = redis_client
        self.origin_worker_id = origin_worker_id or f"worker_{uuid4().hex}"
        self.runtime_metrics = runtime_metrics
        self.status = PubSubStatus(enabled=bool(settings.redis_pubsub_enabled), connected=False)
        self._handlers: dict[str, list[PubSubHandler]] = {kind: [] for kind in self.CHANNEL_TYPES}
        self._task: asyncio.Task | None = None
        self._pubsub = None

    def register_caption_handler(self, handler: PubSubHandler) -> None:
        self._handlers["captions"].append(handler)

    def register_debug_handler(self, handler: PubSubHandler) -> None:
        self._handlers["debug"].append(handler)
        self._handlers["diagnostics"].append(handler)

    def register_question_handler(self, handler: PubSubHandler) -> None:
        self._handlers["questions"].append(handler)

    def channel(self, lesson_id: str, kind: str) -> str:
        return build_redis_key(self.settings, "lesson", lesson_id, kind)

    async def publish_caption(self, lesson_id: str, payload: dict) -> bool:
        return await self.publish(lesson_id, "captions", payload)

    async def publish_debug(self, lesson_id: str, payload: dict) -> bool:
        kind = "diagnostics" if payload.get("event") == "diagnostic" else "debug"
        return await self.publish(lesson_id, kind, payload)

    async def publish_question(self, lesson_id: str, payload: dict) -> bool:
        return await self.publish(lesson_id, "questions", payload)

    async def publish(self, lesson_id: str, kind: str, payload: dict) -> bool:
        if not self.status.enabled or self.redis is None:
            return False
        if self.status.error:
            return False
        try:
            started_at = perf_counter()
            await self.redis.publish(
                self.channel(lesson_id, kind),
                json.dumps(
                    {
                        "origin_worker_id": self.origin_worker_id,
                        "kind": kind,
                        "lesson_id": lesson_id,
                        "published_at": time.time(),
                        "payload": payload,
                    },
                    ensure_ascii=False,
                ),
            )
            self._record_published((perf_counter() - started_at) * 1000)
            self.status.connected = True
            self.status.error = None
            return True
        except Exception as exc:
            self._record_error(exc)
            return False

    async def start(self) -> None:
        if not self.status.enabled or self.redis is None or self._task is not None:
            return
        self._task = asyncio.create_task(self._listen(), name="redis-pubsub-fanout")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._close_pubsub()

    async def dispatch_message(self, channel: str | bytes, message: str | bytes | dict) -> None:
        envelope = _loads_message(message)
        if not envelope:
            return
        kind = envelope.get("kind") or _kind_from_channel(channel)
        lesson_id = envelope.get("lesson_id") or _lesson_from_channel(channel)
        payload = envelope.get("payload")
        if kind not in self.CHANNEL_TYPES or not lesson_id or not isinstance(payload, dict):
            return
        self._record_received(_message_latency_ms(envelope))
        for handler in list(self._handlers.get(kind, [])):
            await handler(lesson_id, payload)

    async def _listen(self) -> None:
        while True:
            try:
                self._pubsub = self.redis.pubsub()
                pattern = build_redis_key(self.settings, "lesson", "*", "*")
                await self._pubsub.psubscribe(pattern)
                self.status.connected = True
                self.status.error = None
                async for message in self._pubsub.listen():
                    if message.get("type") not in {"message", "pmessage"}:
                        continue
                    channel = message.get("channel") or message.get("pattern")
                    await self.dispatch_message(channel, message.get("data"))
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._close_pubsub()
                if _is_pubsub_idle_timeout(exc):
                    self.status.connected = True
                    self.status.error = None
                    await asyncio.sleep(0.05)
                    continue
                self._record_error(exc)
                return

    async def _close_pubsub(self) -> None:
        if self._pubsub is None:
            return
        close = getattr(self._pubsub, "aclose", None) or getattr(self._pubsub, "close", None)
        if close is not None:
            result = close()
            if result is not None:
                await result
        self._pubsub = None

    def _record_error(self, exc: Exception) -> None:
        self.status.connected = False
        self.status.error = sanitize_redis_error(exc, self.settings)
        if self.runtime_metrics is not None:
            self.runtime_metrics.record_redis_pubsub_error()
        logger.warning("redis_pubsub_unavailable", extra={"event": {"error": self.status.error}})

    def _record_published(self, latency_ms: float) -> None:
        if self.runtime_metrics is not None:
            self.runtime_metrics.record_redis_pubsub_published(latency_ms)

    def _record_received(self, latency_ms: float | None) -> None:
        if self.runtime_metrics is not None:
            self.runtime_metrics.record_redis_pubsub_received(latency_ms)


def _loads_message(message: str | bytes | dict) -> dict:
    if isinstance(message, dict):
        return message
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    if not isinstance(message, str):
        return {}
    try:
        return json.loads(message)
    except json.JSONDecodeError:
        return {}


def _kind_from_channel(channel: str | bytes) -> str | None:
    text = channel.decode("utf-8") if isinstance(channel, bytes) else str(channel)
    return text.rsplit(":", 1)[-1] if ":" in text else None


def _lesson_from_channel(channel: str | bytes) -> str | None:
    text = channel.decode("utf-8") if isinstance(channel, bytes) else str(channel)
    parts = text.split(":")
    try:
        lesson_index = parts.index("lesson")
    except ValueError:
        return None
    if lesson_index + 1 >= len(parts):
        return None
    return parts[lesson_index + 1]


def _message_latency_ms(envelope: dict) -> float | None:
    published_at = envelope.get("published_at")
    if not isinstance(published_at, (int, float)):
        return None
    latency_ms = (time.time() - float(published_at)) * 1000
    if latency_ms < 0:
        return None
    return round(latency_ms, 2)


def _is_pubsub_idle_timeout(exc: Exception) -> bool:
    message = str(exc).lower()
    return exc.__class__.__name__ == "TimeoutError" and "reading from socket" in message
