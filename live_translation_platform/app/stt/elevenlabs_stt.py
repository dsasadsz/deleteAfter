import asyncio
import base64
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from app.stt.base import STTEvent, STTProvider


ELEVENLABS_REALTIME_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
ERROR_MESSAGE_TYPES = {
    "auth_error",
    "quota_exceeded",
    "transcriber_error",
    "input_error",
    "error",
    "commit_throttled",
    "unaccepted_terms",
    "rate_limited",
    "queue_overflow",
    "resource_exhausted",
    "session_time_limit_exceeded",
    "chunk_size_exceeded",
    "insufficient_audio_activity",
}


class ProviderConfigurationError(RuntimeError):
    pass


class ProviderConnectionError(RuntimeError):
    pass


class ProviderRuntimeError(RuntimeError):
    pass


WebSocketFactory = Callable[[str, dict[str, str], float], Awaitable[Any]]


class ElevenLabsSTTProvider(STTProvider):
    name = "elevenlabs"
    supports_commit = True

    def __init__(
        self,
        api_key: str,
        model_id: str = "scribe_v2_realtime",
        language: str = "ru",
        audio_format: str = "pcm_16000",
        sample_rate: int = 16000,
        commit_strategy: str = "vad",
        enable_partials: bool = True,
        connect_timeout_seconds: float = 10.0,
        receive_timeout_seconds: float = 30.0,
        max_reconnects: int = 3,
        websocket_client_factory: WebSocketFactory | None = None,
    ) -> None:
        self.api_key = api_key
        self.model_id = model_id
        self.language = language
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.commit_strategy = commit_strategy
        self.enable_partials = enable_partials
        self.connect_timeout_seconds = connect_timeout_seconds
        self.receive_timeout_seconds = receive_timeout_seconds
        self.max_reconnects = max_reconnects
        self.websocket_client_factory = websocket_client_factory or _websockets_connect
        self.websocket: Any | None = None
        self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._receive_task: asyncio.Task | None = None
        self._audio_timestamp_queue: asyncio.Queue[datetime] = asyncio.Queue()
        self.connected_at: datetime | None = None
        self.audio_chunks_sent = 0
        self.partial_events_received = 0
        self.final_events_received = 0
        self.last_event_at: datetime | None = None
        self.errors_count = 0
        self.last_error: str | None = None
        self.provider_connected_at: datetime | None = None
        self.first_audio_chunk_provider_sent_at: datetime | None = None
        self.last_audio_chunk_provider_sent_at: datetime | None = None
        self.first_partial_received_at: datetime | None = None
        self.first_final_received_at: datetime | None = None
        self.finalize_sent_at: datetime | None = None
        self.provider_closed_at: datetime | None = None
        self._closed = False
        self._events_closed = False

    async def connect(self) -> None:
        if not self.api_key:
            raise ProviderConfigurationError("Missing ELEVENLABS_API_KEY for STT_PROVIDER=elevenlabs.")
        try:
            self.websocket = await self.websocket_client_factory(
                self._uri(),
                {"xi-api-key": self.api_key},
                self.connect_timeout_seconds,
            )
        except Exception as exc:
            raise ProviderConnectionError(f"ElevenLabs STT WebSocket connection failed: {exc}") from exc
        self.connected_at = datetime.utcnow()
        self.provider_connected_at = self.connected_at
        self._closed = False
        self._events_closed = False
        self._receive_task = asyncio.create_task(self._receive_loop(), name="elevenlabs-stt-receive")
        await asyncio.sleep(0.01)

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        if self.websocket is None or self._closed:
            self._record_error("ElevenLabs STT WebSocket is not connected.")
            raise ProviderConnectionError("ElevenLabs STT WebSocket is not connected.")
        metadata = metadata or {}
        sample_rate = metadata.get("sample_rate") or self.sample_rate
        audio_format = metadata.get("format") or self.audio_format
        if sample_rate != self.sample_rate or audio_format not in {"L16", self.audio_format, "pcm_16000"}:
            self.last_error = f"Audio format warning: got sample_rate={sample_rate}, format={audio_format}; no resampling in Stage 4B."
        await self._audio_timestamp_queue.put(metadata.get("audio_received_at") or datetime.utcnow())
        message = {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(audio_chunk).decode(),
            "sample_rate": sample_rate,
        }
        if self.commit_strategy == "manual":
            message["commit"] = False
        if metadata.get("finalize") and self.commit_strategy == "manual" and metadata.get("finalize_mode") == "inline":
            message["commit"] = True
        await self.websocket.send(json.dumps(message))
        sent_at = datetime.utcnow()
        self.first_audio_chunk_provider_sent_at = self.first_audio_chunk_provider_sent_at or sent_at
        self.last_audio_chunk_provider_sent_at = sent_at
        self.audio_chunks_sent += 1
        if metadata.get("finalize") and self.commit_strategy == "manual" and metadata.get("finalize_mode") == "inline":
            self.finalize_sent_at = sent_at
        if metadata.get("finalize") and self.commit_strategy == "manual" and metadata.get("finalize_mode") != "inline":
            await self.websocket.send(
                json.dumps(
                    {
                        "message_type": "input_audio_chunk",
                        "audio_base_64": "",
                        "sample_rate": sample_rate,
                        "commit": True,
                    }
                )
            )
            self.finalize_sent_at = datetime.utcnow()

    async def commit(self, reason: str | None = None) -> None:
        if self.websocket is None or self._closed:
            self._record_error("ElevenLabs STT WebSocket is not connected.")
            raise ProviderConnectionError("ElevenLabs STT WebSocket is not connected.")
        await self.websocket.send(
            json.dumps(
                {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": "",
                    "sample_rate": self.sample_rate,
                    "commit": True,
                }
            )
        )
        self.finalize_sent_at = datetime.utcnow()

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                break
            yield event

    async def close(self) -> None:
        if self._closed and self.provider_closed_at is not None:
            await self._close_events_once()
            return
        self._closed = True
        if self._receive_task:
            self._receive_task.cancel()
            await asyncio.gather(self._receive_task, return_exceptions=True)
        if self.websocket is not None:
            try:
                await self.websocket.close()
            except Exception as exc:
                self._record_error(f"ElevenLabs STT WebSocket close failed: {exc}")
            self.websocket = None
        self.provider_closed_at = datetime.utcnow()
        await self._close_events_once()

    async def _receive_loop(self) -> None:
        while not self._closed:
            try:
                raw = await self.websocket.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closed:
                    break
                self._record_error(f"ElevenLabs STT receive failed: {exc}")
                await self._close_events_once()
                break
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                self._record_error("Invalid JSON from ElevenLabs STT")
                continue
            if payload.get("message_type") in ERROR_MESSAGE_TYPES:
                self._record_error(str(payload.get("message") or payload.get("error") or payload.get("message_type")))
                continue
            event = parse_elevenlabs_message(payload, await self._latest_audio_timestamp(), self.language)
            if event is None:
                continue
            if event.is_partial:
                self.partial_events_received += 1
                self.first_partial_received_at = self.first_partial_received_at or event.timestamp
            if event.is_final:
                self.final_events_received += 1
                self.first_final_received_at = self.first_final_received_at or event.timestamp
            self.last_event_at = event.timestamp
            await self._events.put(event)

    async def _latest_audio_timestamp(self) -> datetime:
        latest = datetime.utcnow()
        while not self._audio_timestamp_queue.empty():
            latest = await self._audio_timestamp_queue.get()
        return latest

    def _record_error(self, message: str) -> None:
        self.errors_count += 1
        self.last_error = message

    async def _close_events_once(self) -> None:
        if self._events_closed:
            return
        self._events_closed = True
        await self._events.put(None)

    def _uri(self) -> str:
        query = {
            "model_id": self.model_id,
            "language_code": self.language,
            "audio_format": self.audio_format,
            "commit_strategy": self.commit_strategy,
            "include_timestamps": "true",
        }
        return f"{ELEVENLABS_REALTIME_URL}?{urlencode(query)}"


def parse_elevenlabs_message(payload: dict, audio_received_at: datetime, language: str) -> STTEvent | None:
    message_type = payload.get("message_type")
    text = payload.get("text") or ""
    if message_type == "partial_transcript":
        is_partial = True
        is_final = False
    elif message_type in {"committed_transcript", "committed_transcript_with_timestamps"}:
        is_partial = False
        is_final = True
    else:
        return None
    if not text:
        return None
    return STTEvent(
        text=text,
        is_partial=is_partial,
        is_final=is_final,
        language=payload.get("language_code") or language,
        confidence=None,
        provider="elevenlabs",
        timestamp=datetime.utcnow(),
        speaker_id=None,
        raw=payload,
        audio_received_at=audio_received_at,
    )


async def _websockets_connect(uri: str, headers: dict[str, str], timeout: float):
    import websockets

    try:
        return await asyncio.wait_for(websockets.connect(uri, additional_headers=headers), timeout=timeout)
    except TypeError:
        return await asyncio.wait_for(websockets.connect(uri, extra_headers=headers), timeout=timeout)


ElevenLabsSTT = ElevenLabsSTTProvider
