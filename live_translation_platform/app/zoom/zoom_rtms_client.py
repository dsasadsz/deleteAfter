import asyncio
import importlib.util
from datetime import datetime
from typing import Any


class RTMSUnavailableError(RuntimeError):
    pass


class ZoomRTMSClient:
    def __init__(
        self,
        lesson_id: str,
        webhook_payload: dict,
        event_queue: asyncio.Queue,
        enabled: bool,
    ) -> None:
        self.lesson_id = lesson_id
        self.webhook_payload = webhook_payload
        self.event_queue = event_queue
        self.enabled = enabled
        self.sdk_available = importlib.util.find_spec("rtms") is not None
        self.client: Any | None = None
        self.connected = False

    async def connect_from_webhook(self) -> None:
        if not self.enabled:
            raise RTMSUnavailableError("RTMS disabled or SDK not installed")
        if not self.sdk_available:
            raise RTMSUnavailableError("RTMS disabled or SDK not installed")
        rtms = __import__("rtms")
        self.client = rtms.Client()
        self._register_callbacks()
        self.client.join(self.webhook_payload.get("payload", self.webhook_payload))
        self.connected = True
        await self.event_queue.put({"kind": "session", "status": "connected", "timestamp": datetime.utcnow()})

    async def close(self) -> None:
        if self.client and hasattr(self.client, "leave"):
            self.client.leave()
        self.connected = False

    def on_audio_data(self, data: bytes, timestamp: Any = None, metadata: Any = None) -> None:
        self._enqueue_threadsafe(
            {
                "kind": "audio",
                "data": data,
                "timestamp": datetime.utcnow(),
                "metadata": _metadata_to_dict(metadata) | {"rtms_timestamp": str(timestamp)},
            }
        )

    def on_transcript_data(self, data: bytes, timestamp: Any = None, metadata: Any = None) -> None:
        text = data.decode(errors="ignore") if isinstance(data, bytes) else str(data)
        self._enqueue_threadsafe(
            {
                "kind": "transcript",
                "text": text,
                "timestamp": datetime.utcnow(),
                "metadata": _metadata_to_dict(metadata) | {"rtms_timestamp": str(timestamp)},
            }
        )

    def on_participant_event(self, *args: Any) -> None:
        self._enqueue_threadsafe({"kind": "participant", "timestamp": datetime.utcnow(), "metadata": {"args": [str(arg) for arg in args]}})

    def on_session_event(self, *args: Any) -> None:
        self._enqueue_threadsafe({"kind": "session", "timestamp": datetime.utcnow(), "metadata": {"args": [str(arg) for arg in args]}})

    def _register_callbacks(self) -> None:
        if self.client is None:
            return
        for name, callback in [
            ("on_audio_data", self.on_audio_data),
            ("on_transcript_data", self.on_transcript_data),
            ("on_participant_event", self.on_participant_event),
            ("on_session_event", self.on_session_event),
        ]:
            hook = getattr(self.client, name, None)
            if callable(hook):
                hook(callback)

    def _enqueue_threadsafe(self, event: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(self.event_queue.put_nowait, event)
        except RuntimeError:
            self.event_queue.put_nowait(event)


def _metadata_to_dict(metadata: Any) -> dict:
    if metadata is None:
        return {}
    if isinstance(metadata, dict):
        return metadata
    return {
        key: str(getattr(metadata, key))
        for key in dir(metadata)
        if not key.startswith("_") and not callable(getattr(metadata, key))
    }
