from __future__ import annotations

import asyncio
import json
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.loadtest.audio_normalizer import normalize_lesson_audio
from app.loadtest.report_builder import sanitize_for_report


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


@dataclass
class TeacherBotConfig:
    lesson_id: str
    audio_bytes: bytes | None = None
    audio_path: str | Path | None = None
    chunk_ms: int = 50
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2
    normalize_audio: bool = True
    force_commit_every_seconds: float = 3.0
    realtime: bool = True
    speedup: float = 1.0


@dataclass
class TeacherStreamResult:
    lesson_id: str
    chunks_sent: int
    bytes_sent: int
    commit_markers_sent: int
    duration_seconds: float


class StudentCaptionEvent(BaseModel):
    student_id: str
    lesson_id: str
    caption_sequence: int = 0
    caption_id: str | None = None
    is_final: bool = False
    source_text: str = ""
    translations: dict[str, str] = Field(default_factory=dict)
    provider_latency_ms: dict[str, float] = Field(default_factory=dict)
    received_at: datetime
    student_receive_latency_ms: float | None = None


class TtsEvent(BaseModel):
    student_id: str
    lesson_id: str
    caption_id: str | None = None
    language: str
    status_code: int
    latency_ms: float
    cache_status: str
    cached: bool
    audio_url: str | None = None
    error: str | None = None
    requested_at: str
    responded_at: str


class VirtualTeacherBot:
    def __init__(self, config: TeacherBotConfig) -> None:
        self.config = config

    async def stream(self, websocket) -> TeacherStreamResult:
        audio_bytes, sample_rate, channels, sample_width = self._audio_payload()
        chunk_bytes = max(1, int(sample_rate * channels * sample_width * (self.config.chunk_ms / 1000.0)))
        await websocket.send_text(
            json.dumps(
                {
                    "event": "audio_metadata",
                    "sample_rate": sample_rate,
                    "channels": channels,
                    "format": "pcm_s16le" if sample_width == 2 else "pcm",
                    "chunk_ms": self.config.chunk_ms,
                    "source": "virtual_teacher_bot",
                    "client_started_at": utc_now_iso(),
                }
            )
        )
        chunks_sent = 0
        bytes_sent = 0
        commit_markers_sent = 0
        next_commit_offset = float(self.config.force_commit_every_seconds or 0)
        total_duration = len(audio_bytes) / max(1, sample_rate * channels * sample_width)
        for offset, chunk in enumerate(_chunks(audio_bytes, chunk_bytes)):
            offset_seconds = (offset * chunk_bytes) / max(1, sample_rate * channels * sample_width)
            await websocket.send_text(json.dumps({"event": "audio_chunk", "client_sent_at": utc_now_iso(), "audio_offset_ms": int(offset_seconds * 1000)}))
            await websocket.send_bytes(chunk)
            chunks_sent += 1
            bytes_sent += len(chunk)
            chunk_end = offset_seconds + (len(chunk) / max(1, sample_rate * channels * sample_width))
            if next_commit_offset and chunk_end >= next_commit_offset:
                await websocket.send_text(json.dumps({"event": "force_commit", "reason": "virtual_lesson_load_test", "audio_offset_ms": int(chunk_end * 1000)}))
                commit_markers_sent += 1
                while next_commit_offset and next_commit_offset <= chunk_end:
                    next_commit_offset += float(self.config.force_commit_every_seconds or 0)
            if self.config.realtime:
                sleep_seconds = (self.config.chunk_ms / 1000.0) / max(0.01, float(self.config.speedup or 1.0))
                await asyncio.sleep(sleep_seconds)
        return TeacherStreamResult(
            lesson_id=self.config.lesson_id,
            chunks_sent=chunks_sent,
            bytes_sent=bytes_sent,
            commit_markers_sent=commit_markers_sent,
            duration_seconds=round(total_duration, 3),
        )

    def _audio_payload(self) -> tuple[bytes, int, int, int]:
        if self.config.audio_bytes is not None:
            return self.config.audio_bytes, self.config.sample_rate, self.config.channels, self.config.sample_width
        if self.config.audio_path is None:
            return b"", self.config.sample_rate, self.config.channels, self.config.sample_width
        path = Path(self.config.audio_path)
        if self.config.normalize_audio:
            normalized = normalize_lesson_audio(path, output_dir=path.parent / ".normalized")
            return normalized.pcm_path.read_bytes(), normalized.sample_rate, normalized.channels, normalized.sample_width
        if path.suffix.lower() == ".wav":
            with wave.open(str(path), "rb") as reader:
                return reader.readframes(reader.getnframes()), reader.getframerate(), reader.getnchannels(), reader.getsampwidth()
        return path.read_bytes(), self.config.sample_rate, self.config.channels, self.config.sample_width


class VirtualStudentBot:
    def __init__(self, student_id: str, lesson_id: str) -> None:
        self.student_id = student_id
        self.lesson_id = lesson_id
        self.events: list[StudentCaptionEvent] = []
        self.connected = False
        self.disconnect_reason: str | None = None
        self.error: str | None = None

    @property
    def captions_received(self) -> int:
        return len(self.events)

    def record_caption(self, payload: dict[str, Any], *, received_at: datetime | None = None) -> StudentCaptionEvent:
        timestamp = received_at or utc_now()
        sequence = int(payload.get("sequence") or payload.get("caption_sequence") or 0)
        provider_latency = {key: float(value) for key, value in dict(payload.get("latency_ms") or {}).items() if _is_number(value)}
        event = StudentCaptionEvent(
            student_id=self.student_id,
            lesson_id=str(payload.get("lesson_id") or self.lesson_id),
            caption_sequence=sequence,
            caption_id=payload.get("caption_id") or payload.get("event_id") or f"caption_{sequence}",
            is_final=bool(payload.get("is_final", not payload.get("is_partial", False))),
            source_text=str(payload.get("source_text") or payload.get("original_text") or ""),
            translations=dict(payload.get("translations") or {}),
            provider_latency_ms=provider_latency,
            received_at=timestamp,
            student_receive_latency_ms=_receive_latency_ms(payload, timestamp),
        )
        self.events.append(event)
        return event

    async def consume_messages(self, messages) -> None:
        self.connected = True
        try:
            async for raw in messages:
                payload = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(payload, dict):
                    self.record_caption(payload)
        except Exception as exc:
            self.error = exc.__class__.__name__


class TtsRequestPlanner:
    def __init__(self, request_ratio: float) -> None:
        self.request_ratio = max(0.0, min(1.0, float(request_ratio)))

    def should_request(self, *, student_index: int, total_students: int) -> bool:
        if self.request_ratio <= 0 or total_students <= 0:
            return False
        if self.request_ratio >= 1:
            return True
        selected_count = max(1, int(total_students * self.request_ratio))
        selected_indexes = {round(index * total_students / selected_count) for index in range(selected_count)}
        return student_index in selected_indexes


class VirtualTtsBot:
    def __init__(
        self,
        *,
        student_id: str,
        lesson_id: str,
        language: str,
        http_client,
        enabled: bool = True,
        bypass_rate_limit: bool = False,
    ) -> None:
        self.student_id = student_id
        self.lesson_id = lesson_id
        self.language = language
        self.http_client = http_client
        self.enabled = enabled
        self.bypass_rate_limit = bypass_rate_limit
        self.events: list[TtsEvent] = []

    async def request_tts(self, caption: StudentCaptionEvent) -> TtsEvent:
        requested_at = utc_now_iso()
        if not self.enabled:
            event = TtsEvent(
                student_id=self.student_id,
                lesson_id=self.lesson_id,
                caption_id=caption.caption_id,
                language=self.language,
                status_code=0,
                latency_ms=0,
                cache_status="skipped",
                cached=False,
                requested_at=requested_at,
                responded_at=requested_at,
            )
            self.events.append(event)
            return event
        text = caption.translations.get(self.language) or caption.source_text
        payload = {
            "lesson_id": self.lesson_id,
            "caption_id": caption.caption_id,
            "sequence": caption.caption_sequence,
            "text": text,
            "language": self.language,
            "return_mode": "url",
        }
        headers = {"x-tts-load-test-bypass-rate-limit": "true"} if self.bypass_rate_limit else {}
        try:
            response = await self.http_client.post_json(f"/api/lessons/{self.lesson_id}/tts/synthesize", payload, headers=headers)
            responded_at = utc_now_iso()
            body = dict(response.get("json") or {})
            headers = {str(key).lower(): str(value).lower() for key, value in dict(response.get("headers") or {}).items()}
            cached = bool(body.get("cached")) or headers.get("x-tts-cache") == "hit"
            event = TtsEvent(
                student_id=self.student_id,
                lesson_id=self.lesson_id,
                caption_id=caption.caption_id,
                language=self.language,
                status_code=int(response.get("status") or response.get("status_code") or 200),
                latency_ms=float(response.get("latency_ms") or 0),
                cache_status="hit" if cached else "miss",
                cached=cached,
                audio_url=sanitize_for_report(body.get("audio_url")),
                requested_at=requested_at,
                responded_at=responded_at,
            )
        except Exception as exc:
            responded_at = utc_now_iso()
            event = TtsEvent(
                student_id=self.student_id,
                lesson_id=self.lesson_id,
                caption_id=caption.caption_id,
                language=self.language,
                status_code=0,
                latency_ms=0,
                cache_status="error",
                cached=False,
                error=sanitize_for_report(str(exc) or exc.__class__.__name__),
                requested_at=requested_at,
                responded_at=responded_at,
            )
        self.events.append(event)
        return event


def _chunks(data: bytes, size: int):
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _receive_latency_ms(payload: dict[str, Any], received_at: datetime) -> float | None:
    timestamps = payload.get("timestamps") if isinstance(payload.get("timestamps"), dict) else {}
    candidate = payload.get("caption_broadcast_at") or payload.get("load_test_published_at") or timestamps.get("websocket_sent_at")
    if not candidate:
        return None
    try:
        sent_at = datetime.fromisoformat(str(candidate).replace("Z", "+00:00"))
    except ValueError:
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return round(max(0.0, (received_at - sent_at.astimezone(timezone.utc)).total_seconds() * 1000), 2)
