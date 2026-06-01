import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from app.realtime.metrics import milliseconds_between
from app.stt.base import STTEvent, STTProvider


CARTESIA_STT_WEBSOCKET_URL = "wss://api.cartesia.ai/stt/websocket"


class CartesiaSTTConfigurationError(RuntimeError):
    pass


class CartesiaSTTConnectionError(RuntimeError):
    pass


class CartesiaSTTRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class CartesiaProviderError:
    title: str
    message: str
    error_code: str
    raw: dict


WebSocketFactory = Callable[[str, dict[str, str], float], Awaitable[Any]]


class CartesiaSTTProvider(STTProvider):
    name = "cartesia"

    def __init__(
        self,
        api_key: str,
        model: str = "ink-whisper",
        language: str = "ru",
        encoding: str = "pcm_s16le",
        sample_rate: int = 16000,
        enable_partials: bool = True,
        max_reconnects: int = 3,
        connect_timeout_seconds: float = 10.0,
        receive_timeout_seconds: float = 30.0,
        version: str = "2025-04-16",
        endpoint: str = CARTESIA_STT_WEBSOCKET_URL,
        min_volume: float = 0.1,
        max_silence_duration_secs: float = 0.8,
        websocket_client_factory: WebSocketFactory | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.language = language
        self.encoding = encoding
        self.sample_rate = sample_rate
        self.enable_partials = enable_partials
        self.max_reconnects = max_reconnects
        self.connect_timeout_seconds = connect_timeout_seconds
        self.receive_timeout_seconds = receive_timeout_seconds
        self.version = version
        self.endpoint = endpoint
        self.min_volume = min_volume
        self.max_silence_duration_secs = max_silence_duration_secs
        self.websocket_client_factory = websocket_client_factory or _websockets_connect
        self.websocket: Any | None = None
        self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._receive_task: asyncio.Task | None = None
        self._audio_timestamp_queue: asyncio.Queue[datetime] = asyncio.Queue()
        self._closed = False
        self.connected_at: datetime | None = None
        self.audio_chunks_sent = 0
        self.audio_bytes_sent = 0
        self.partial_events_received = 0
        self.final_events_received = 0
        self.errors_count = 0
        self.last_error: str | None = None
        self.last_event_at: datetime | None = None
        self.last_transcript: str | None = None
        self._latencies_ms: list[int] = []
        self._events_closed = False

    @property
    def cartesia_connected_at(self) -> datetime | None:
        return self.connected_at

    @property
    def cartesia_audio_chunks_sent(self) -> int:
        return self.audio_chunks_sent

    @property
    def cartesia_audio_bytes_sent(self) -> int:
        return self.audio_bytes_sent

    @property
    def cartesia_partial_events_received(self) -> int:
        return self.partial_events_received

    @property
    def cartesia_final_events_received(self) -> int:
        return self.final_events_received

    @property
    def cartesia_last_event_at(self) -> datetime | None:
        return self.last_event_at

    @property
    def cartesia_errors_count(self) -> int:
        return self.errors_count

    @property
    def cartesia_last_error(self) -> str | None:
        return self.last_error

    @property
    def stt_provider_latency_ms(self) -> float:
        return round(sum(self._latencies_ms) / len(self._latencies_ms), 1) if self._latencies_ms else 0.0

    async def connect(self) -> None:
        if not self.api_key:
            raise CartesiaSTTConfigurationError("Missing CARTESIA_API_KEY for STT_PROVIDER=cartesia.")
        try:
            self.websocket = await self.websocket_client_factory(
                self._uri(),
                {"Authorization": f"Bearer {self.api_key}", "Cartesia-Version": self.version},
                self.connect_timeout_seconds,
            )
        except Exception as exc:
            self.last_error = f"Cartesia STT WebSocket connection failed: {exc}"
            raise CartesiaSTTConnectionError(self.last_error) from exc
        self.connected_at = datetime.utcnow()
        self._closed = False
        self._events_closed = False
        self._receive_task = asyncio.create_task(self._receive_loop(), name="cartesia-stt-receive")

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        if self.websocket is None:
            raise CartesiaSTTConnectionError("Cartesia STT WebSocket is not connected.")
        metadata = metadata or {}
        sample_rate = metadata.get("sample_rate") or self.sample_rate
        audio_format = metadata.get("format") or self.encoding
        channels = metadata.get("channels") or 1
        if sample_rate != self.sample_rate or audio_format not in {self.encoding, "L16", "pcm_16000"} or channels != 1:
            self.last_error = (
                "Audio format warning: Cartesia STT expects PCM signed 16-bit little-endian mono at "
                f"{self.sample_rate} Hz; got sample_rate={sample_rate}, channels={channels}, format={audio_format}. "
                "Stage 6B does not resample."
            )
        await self._audio_timestamp_queue.put(metadata.get("audio_received_at") or datetime.utcnow())
        await self.websocket.send(audio_chunk)
        self.audio_chunks_sent += 1
        self.audio_bytes_sent += len(audio_chunk)

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                break
            yield event

    async def close(self) -> None:
        self._closed = True
        if self.websocket is not None:
            try:
                await self.websocket.send("done")
            except Exception:
                pass
        if self._receive_task:
            self._receive_task.cancel()
            await asyncio.gather(self._receive_task, return_exceptions=True)
        if self.websocket is not None:
            await self.websocket.close()
        await self._close_events_once()

    async def _receive_loop(self) -> None:
        while not self._closed:
            try:
                raw = await asyncio.wait_for(self.websocket.recv(), timeout=self.receive_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._closed:
                    self._record_error(f"Cartesia STT receive failed: {exc}")
                    await self._close_events_once()
                break
            message = self._decode_message(raw)
            if message is None:
                self._record_error("Invalid JSON from Cartesia STT")
                continue
            if message.get("type") == "error":
                error = parse_cartesia_error(message)
                self._record_error(error.message)
                continue
            event = parse_cartesia_message(message, await self._latest_audio_timestamp(), self.language)
            if event is None:
                continue
            if event.is_partial:
                self.partial_events_received += 1
            if event.is_final:
                self.final_events_received += 1
            self.last_event_at = event.timestamp
            self.last_transcript = event.text
            if event.audio_received_at is not None:
                self._latencies_ms.append(milliseconds_between(event.audio_received_at, event.timestamp))
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

    def _decode_message(self, raw: Any) -> dict | None:
        if isinstance(raw, bytes):
            raw = raw.decode(errors="ignore")
        if isinstance(raw, dict):
            return raw
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _uri(self) -> str:
        query = {
            "model": self.model,
            "language": self.language,
            "encoding": self.encoding,
            "sample_rate": str(self.sample_rate),
            "min_volume": str(self.min_volume),
            "max_silence_duration_secs": str(self.max_silence_duration_secs),
        }
        return f"{self.endpoint}?{urlencode(query)}"


def parse_cartesia_message(message: dict, audio_received_at: datetime, language: str) -> STTEvent | None:
    message_type = str(message.get("type") or "").lower()
    text = str(message.get("text") or message.get("transcript") or "").strip()
    if message_type in {"error", "flush_done", "done"} or not text:
        return None
    is_final = bool(message.get("is_final")) or message_type == "final"
    is_partial = not is_final and message_type in {"transcript", "partial", "interim"}
    if not is_partial and not is_final:
        return None
    return STTEvent(
        text=text,
        is_partial=is_partial,
        is_final=is_final,
        language=str(message.get("language") or language),
        confidence=None,
        provider="cartesia",
        timestamp=datetime.utcnow(),
        speaker_id="teacher",
        raw=message,
        audio_received_at=audio_received_at,
    )


def parse_cartesia_error(message: dict) -> CartesiaProviderError:
    return CartesiaProviderError(
        title=str(message.get("title") or "Cartesia STT error"),
        message=str(message.get("message") or message.get("error") or message.get("title") or "Cartesia STT error"),
        error_code=str(message.get("error_code") or ""),
        raw=message,
    )


async def _websockets_connect(uri: str, headers: dict[str, str], timeout: float):
    import websockets

    try:
        return await asyncio.wait_for(websockets.connect(uri, additional_headers=headers), timeout=timeout)
    except TypeError:
        return await asyncio.wait_for(websockets.connect(uri, extra_headers=headers), timeout=timeout)


CartesiaSTT = CartesiaSTTProvider
