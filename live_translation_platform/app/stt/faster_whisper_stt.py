from __future__ import annotations

import asyncio
import os
import tempfile
import wave
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from app.stt.base import STTEvent, STTProvider


class FasterWhisperBackend(Protocol):
    def load(self) -> None:
        ...

    def transcribe(self, wav_path: str, *, language: str | None, beam_size: int, vad_filter: bool) -> dict:
        ...


class CTranslate2FasterWhisperBackend:
    def __init__(self, *, model_path: str, device: str, compute_type: str) -> None:
        self.model_path = model_path
        self.device = device
        self.compute_type = compute_type
        self._model: Any | None = None

    def load(self) -> None:
        if self._model is not None:
            return
        if not self.model_path:
            raise FasterWhisperConfigurationError("Missing FASTER_WHISPER_MODEL_PATH")
        if not Path(self.model_path).exists():
            raise FasterWhisperConfigurationError(f"FasterWhisper model path does not exist: {self.model_path}")
        from faster_whisper import WhisperModel

        self._model = WhisperModel(self.model_path, device=self.device, compute_type=self.compute_type)

    def transcribe(self, wav_path: str, *, language: str | None, beam_size: int, vad_filter: bool) -> dict:
        self.load()
        segments, info = self._model.transcribe(  # type: ignore[union-attr]
            wav_path,
            language=language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return {
            "text": text,
            "language": getattr(info, "language", language or ""),
            "confidence": getattr(info, "language_probability", None),
        }


class FasterWhisperConfigurationError(RuntimeError):
    pass


class FasterWhisperSTTProvider(STTProvider):
    name = "faster_whisper"
    supports_commit = True

    def __init__(
        self,
        *,
        model_path: str = "",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "ru",
        beam_size: int = 1,
        vad_filter: bool = False,
        timeout_seconds: float = 10.0,
        load_on_startup: bool = False,
        disable_auto_language_detection: bool = True,
        segment_seconds: float = 5.0,
        backend: FasterWhisperBackend | None = None,
    ) -> None:
        self.model_path = model_path or ""
        self.device = device or "cpu"
        self.compute_type = compute_type or "int8"
        self.language = normalize_language(language or "ru")
        self.beam_size = int(beam_size or 1)
        self.vad_filter = bool(vad_filter)
        self.timeout_seconds = float(timeout_seconds or 10.0)
        self.load_on_startup = bool(load_on_startup)
        self.disable_auto_language_detection = bool(disable_auto_language_detection)
        self.segment_seconds = float(segment_seconds or 5.0)
        self._backend = backend
        self._loaded = False
        self._connected = False
        self._events: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._buffer = bytearray()
        self._latest_metadata: dict | None = None
        self._buffer_started_at: datetime | None = None
        self._buffer_audio_received_at: datetime | None = None
        self.connected_at: datetime | None = None
        self.session_started_at: datetime | None = None
        self.session_stopped_at: datetime | None = None
        self.provider_connected_at: datetime | None = None
        self.audio_chunks_sent = 0
        self.audio_bytes_sent = 0
        self.partial_events_received = 0
        self.final_events_received = 0
        self.no_match_count = 0
        self.canceled_count = 0
        self.errors_count = 0
        self.last_event_at: datetime | None = None
        self.last_error: str | None = None
        self.last_transcript: str | None = None
        self.first_audio_chunk_provider_sent_at: datetime | None = None
        self.last_audio_chunk_provider_sent_at: datetime | None = None
        self.first_final_received_at: datetime | None = None
        self.finalize_sent_at: datetime | None = None
        self.provider_closed_at: datetime | None = None
        self._latencies_ms: list[float] = []

    async def connect(self) -> None:
        self._validate_config()
        self._connected = True
        self.connected_at = datetime.utcnow()
        self.provider_connected_at = self.connected_at
        self.session_started_at = self.connected_at
        if self.load_on_startup:
            try:
                await asyncio.to_thread(self._load_backend)
            except Exception as exc:
                self.errors_count += 1
                self.last_error = self._sanitize(str(exc) or exc.__class__.__name__)
                raise RuntimeError(self.last_error) from exc

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        if not self._connected:
            await self.connect()
        metadata = metadata or {}
        now = datetime.utcnow()
        if self.first_audio_chunk_provider_sent_at is None:
            self.first_audio_chunk_provider_sent_at = now
        self.last_audio_chunk_provider_sent_at = now
        self.audio_chunks_sent += 1
        self.audio_bytes_sent += len(audio_chunk)
        if audio_chunk:
            if not self._buffer:
                self._buffer_started_at = now
                self._buffer_audio_received_at = _metadata_datetime(metadata.get("audio_received_at")) or now
            self._buffer.extend(audio_chunk)
            self._latest_metadata = dict(metadata)
        if self._buffer_duration_seconds(metadata) >= self.segment_seconds:
            await self._transcribe_buffer(reason="segment_threshold")

    async def commit(self, reason: str | None = None) -> None:
        self.finalize_sent_at = datetime.utcnow()
        await self._transcribe_buffer(reason=reason or "commit")

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                break
            yield event

    async def close(self) -> None:
        self._connected = False
        self.session_stopped_at = datetime.utcnow()
        self.provider_closed_at = self.session_stopped_at
        await self._events.put(None)

    def status(self) -> dict:
        missing = []
        if not self.model_path:
            missing.append("FASTER_WHISPER_MODEL_PATH")
        if missing:
            status = "not_configured"
        elif self.last_error:
            status = "error"
        elif self._loaded:
            status = "loaded"
        else:
            status = "configured"
        return {
            "ready": not missing and not self.last_error,
            "status": status,
            "missing": missing,
            "configured": not missing,
            "loaded": self._loaded,
            "model_path": "<configured>" if self.model_path else "",
            "device": self.device,
            "compute_type": self.compute_type,
            "language": self.language,
            "auto_language_detection": not self.disable_auto_language_detection,
            "beam_size": self.beam_size,
            "vad_filter": self.vad_filter,
            "timeout_seconds": self.timeout_seconds,
            "load_on_startup": self.load_on_startup,
            "average_latency_ms": self.stt_provider_latency_ms,
            "last_error": self.last_error,
            "final_events_received": self.final_events_received,
            "audio_chunks_sent": self.audio_chunks_sent,
            "audio_bytes_sent": self.audio_bytes_sent,
        }

    @property
    def stt_provider_latency_ms(self) -> float | None:
        if not self._latencies_ms:
            return None
        return sum(self._latencies_ms) / len(self._latencies_ms)

    async def _transcribe_buffer(self, *, reason: str) -> None:
        if not self._buffer:
            return
        metadata = self._latest_metadata or {}
        audio = bytes(self._buffer)
        self._buffer.clear()
        self._latest_metadata = None
        wav_path = ""
        started_at = perf_counter()
        try:
            wav_path = self._write_wav(audio, metadata)
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._backend_for_use().transcribe,
                    wav_path,
                    language=None if not self.disable_auto_language_detection else self.language,
                    beam_size=self.beam_size,
                    vad_filter=self.vad_filter,
                ),
                timeout=self.timeout_seconds,
            )
            latency_ms = (perf_counter() - started_at) * 1000
            self._latencies_ms.append(latency_ms)
            text = str(result.get("text") or "").strip()
            if not text:
                self.no_match_count += 1
                return
            now = datetime.utcnow()
            event = STTEvent(
                text=text,
                is_partial=False,
                is_final=True,
                language=str(result.get("language") or self.language),
                confidence=result.get("confidence"),
                provider=self.name,
                timestamp=now,
                speaker_id=metadata.get("speaker_id") or "teacher",
                raw=self._raw(metadata, reason=reason, latency_ms=latency_ms),
                audio_received_at=self._buffer_audio_received_at or _metadata_datetime(metadata.get("audio_received_at")) or now,
            )
            self.final_events_received += 1
            if self.first_final_received_at is None:
                self.first_final_received_at = now
            self.last_event_at = now
            self.last_transcript = text
            self.last_error = None
            await self._events.put(event)
        except Exception as exc:
            self.errors_count += 1
            self.last_error = self._sanitize(str(exc) or exc.__class__.__name__)
            raise RuntimeError(self.last_error) from exc
        finally:
            self._buffer_started_at = None
            self._buffer_audio_received_at = None
            if wav_path:
                try:
                    os.remove(wav_path)
                except FileNotFoundError:
                    pass

    def _backend_for_use(self) -> FasterWhisperBackend:
        self._load_backend()
        return self._backend  # type: ignore[return-value]

    def _load_backend(self) -> None:
        if self._backend is None:
            self._backend = CTranslate2FasterWhisperBackend(
                model_path=self.model_path,
                device=self.device,
                compute_type=self.compute_type,
            )
        self._backend.load()
        self._loaded = True

    def _validate_config(self) -> None:
        if not self.model_path:
            raise FasterWhisperConfigurationError("Missing FASTER_WHISPER_MODEL_PATH")
        if not self._backend and not Path(self.model_path).exists():
            raise FasterWhisperConfigurationError(f"FasterWhisper model path does not exist: {self._sanitize(self.model_path)}")

    def _write_wav(self, audio: bytes, metadata: dict) -> str:
        sample_rate = int(metadata.get("sample_rate") or 16000)
        channels = int(metadata.get("channels") or 1)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as output:
            output_path = output.name
        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio)
        return output_path

    def _buffer_duration_seconds(self, metadata: dict) -> float:
        sample_rate = int(metadata.get("sample_rate") or 16000)
        channels = int(metadata.get("channels") or 1)
        bytes_per_second = max(1, sample_rate * channels * 2)
        return len(self._buffer) / bytes_per_second

    def _raw(self, metadata: dict, *, reason: str, latency_ms: float) -> dict:
        audio_received_at = metadata.get("audio_received_at")
        return {
            "faster_whisper": True,
            "stage": "final",
            "reason": reason,
            "audio_source": metadata.get("source", "local"),
            "latency_ms": latency_ms,
            "audio": {
                "chunk_timestamp": audio_received_at.isoformat() if hasattr(audio_received_at, "isoformat") else None,
                "sample_rate": metadata.get("sample_rate"),
                "channels": metadata.get("channels"),
                "format": metadata.get("format"),
            },
        }

    def _sanitize(self, message: object) -> str:
        text = str(message)
        if self.model_path:
            text = text.replace(self.model_path, "<model_path>")
            text = text.replace(str(Path(self.model_path)), "<model_path>")
        return text


def normalize_language(language: str) -> str:
    normalized = (language or "ru").strip()
    if normalized.lower() in {"ru-ru", "ru_ru"}:
        return "ru"
    return normalized


def faster_whisper_provider_kwargs(settings) -> dict:
    return {
        "model_path": getattr(settings, "faster_whisper_model_path", ""),
        "device": getattr(settings, "faster_whisper_device", "cpu"),
        "compute_type": getattr(settings, "faster_whisper_compute_type", "int8"),
        "language": getattr(settings, "faster_whisper_language", "") or getattr(settings, "stt_default_language", "ru-RU"),
        "beam_size": getattr(settings, "faster_whisper_beam_size", 1),
        "vad_filter": getattr(settings, "faster_whisper_vad_filter", False),
        "timeout_seconds": getattr(settings, "faster_whisper_timeout_seconds", 10.0),
        "load_on_startup": getattr(settings, "faster_whisper_load_on_startup", False),
        "disable_auto_language_detection": getattr(settings, "disable_auto_language_detection", True),
        "segment_seconds": getattr(settings, "faster_whisper_segment_seconds", 5.0),
    }


def faster_whisper_status(settings) -> dict:
    return FasterWhisperSTTProvider(**faster_whisper_provider_kwargs(settings)).status()


def _metadata_datetime(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None
