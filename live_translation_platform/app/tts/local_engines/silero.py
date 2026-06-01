from __future__ import annotations

import asyncio
import wave
from datetime import datetime
from io import BytesIO
from time import perf_counter
from typing import Protocol

from app.tts.base import TTSConfigurationError
from app.tts.local_engines.base import LocalTTSEngine, LocalTTSSynthesisResult, local_voice, sanitize_tts_error, voice_id_suffix


class SileroBackend(Protocol):
    loaded: bool

    async def load(self) -> None:
        ...

    async def synthesize(self, text: str, language: str, speaker: str | None) -> tuple[bytes, str, int | None]:
        ...


class TorchScriptSileroBackend:
    def __init__(self, *, model_path: str, device: str = "cpu", speaker: str = "", sample_rate: int = 48000) -> None:
        self.model_path = model_path
        self.device = device
        self.speaker = speaker
        self.sample_rate = sample_rate
        self.loaded = False
        self.model = None
        self.torch = None

    async def load(self) -> None:
        if self.loaded:
            return
        await asyncio.to_thread(self._load_sync)

    async def synthesize(self, text: str, language: str, speaker: str | None) -> tuple[bytes, str, int | None]:
        await self.load()
        return await asyncio.to_thread(self._synthesize_sync, text, language, speaker)

    def _load_sync(self) -> None:
        try:
            import torch
        except Exception as exc:
            raise TTSConfigurationError("Python package torch is required for local Silero TTS loading.") from exc
        try:
            self.torch = torch
            self.model = torch.jit.load(self.model_path, map_location=self.device)
            if hasattr(self.model, "eval"):
                self.model.eval()
            self.loaded = True
        except Exception as exc:
            raise TTSConfigurationError(str(exc)) from exc

    def _synthesize_sync(self, text: str, language: str, speaker: str | None) -> tuple[bytes, str, int | None]:
        if self.model is None:
            raise TTSConfigurationError("Silero model is not loaded.")
        try:
            if hasattr(self.model, "apply_tts"):
                audio = self.model.apply_tts(text=text, speaker=speaker or self.speaker or None, sample_rate=self.sample_rate)
            else:
                audio = self.model(text)
            values = audio.detach().cpu().numpy().tolist() if hasattr(audio, "detach") else list(audio)
            wav = _wav_from_float_samples(values, self.sample_rate)
            duration_ms = int(len(values) / self.sample_rate * 1000) if self.sample_rate else None
            return wav, "audio/wav", duration_ms
        except Exception as exc:
            raise TTSConfigurationError(str(exc)) from exc


class SileroTTSEngine(LocalTTSEngine):
    name = "silero"

    def __init__(
        self,
        *,
        enabled: bool = False,
        model_path: str = "",
        device: str = "cpu",
        timeout_seconds: float = 5.0,
        language: str = "ru",
        speaker: str = "",
        backend: SileroBackend | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.model_path = model_path or ""
        self.device = device or "cpu"
        self.timeout_seconds = float(timeout_seconds or 5.0)
        self.language = language or "ru"
        self.speaker = speaker or ""
        self._backend = backend
        self._load_lock = asyncio.Lock()
        self._last_error: str | None = None
        self._load_error: str | None = None
        self._loaded_at: datetime | None = None
        self.request_count = 0
        self.error_count = 0
        self.timeout_count = 0
        self._latencies_ms: list[float] = []

    @property
    def loaded(self) -> bool:
        return bool(self._backend is not None and getattr(self._backend, "loaded", False))

    def supports(self, language: str) -> bool:
        return bool(self.status_for_language(language).get("ready"))

    def default_voice_for_language(self, language: str) -> str:
        return f"silero-{voice_id_suffix(language)}" if language == self.language and self.enabled else ""

    def voice_catalog(self) -> dict[str, list[dict]]:
        voice_id = self.default_voice_for_language(self.language)
        return {self.language: [local_voice(self.language, self.name, voice_id)]} if voice_id else {}

    def status(self) -> dict:
        missing = []
        if self.enabled and not self.model_path and self._backend is None:
            missing.append("SILERO_TTS_MODEL_PATH")
        if not self.enabled:
            status = "disabled"
        elif missing:
            status = "not_configured"
        elif self._load_error:
            status = "error"
        elif self.loaded:
            status = "loaded"
        else:
            status = "configured"
        ready = self.enabled and not missing and not self._load_error
        return {
            "ready": ready,
            "status": status,
            "enabled": self.enabled,
            "missing": missing,
            "engine": self.name,
            "device": self.device,
            "language": self.language,
            "speaker": self.speaker,
            "timeout_seconds": self.timeout_seconds,
            "loaded": self.loaded,
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
            "last_error": self._last_error or self._load_error,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "timeout_count": self.timeout_count,
            "average_latency_ms": self.average_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "content_type": "audio/wav",
            "output_format": "wav",
        }

    def status_for_language(self, language: str) -> dict:
        status = self.status()
        if language != self.language and self.enabled:
            status = dict(status)
            status["ready"] = False
            status["status"] = "not_configured"
            status["missing"] = [f"SILERO_TTS_LANGUAGE_{self.language.upper()}_ONLY"]
        return status

    @property
    def average_latency_ms(self) -> float | None:
        if not self._latencies_ms:
            return None
        return sum(self._latencies_ms) / len(self._latencies_ms)

    @property
    def p95_latency_ms(self) -> float | None:
        if not self._latencies_ms:
            return None
        values = sorted(self._latencies_ms)
        index = max(0, min(len(values) - 1, int(round(0.95 * (len(values) - 1)))))
        return values[index]

    async def synthesize(self, text: str, language: str, voice: str | None = None, audio_format: str | None = None) -> LocalTTSSynthesisResult:
        status = self.status_for_language(language)
        if not status["ready"]:
            raise TTSConfigurationError(f"Silero TTS is not configured: {', '.join(status['missing']) or status['status']}")
        started_at = perf_counter()
        try:
            await asyncio.wait_for(self._ensure_loaded(), timeout=self.timeout_seconds)
            if self._backend is None:
                raise TTSConfigurationError("Silero backend is not configured.")
            audio, content_type, duration_ms = await asyncio.wait_for(
                self._backend.synthesize(text, language, self.speaker or None),
                timeout=self.timeout_seconds,
            )
            self._last_error = None
            return LocalTTSSynthesisResult(audio_bytes=audio, content_type=content_type, duration_ms=duration_ms)
        except asyncio.TimeoutError as exc:
            self.timeout_count += 1
            self.error_count += 1
            self._last_error = self._sanitize(f"Silero TTS timeout after {self.timeout_seconds:g}s")
            raise TTSConfigurationError(self._last_error) from exc
        except Exception as exc:
            self.error_count += 1
            self._last_error = self._sanitize(str(exc) or exc.__class__.__name__)
            raise TTSConfigurationError(self._last_error) from exc
        finally:
            self.request_count += 1
            self._latencies_ms.append((perf_counter() - started_at) * 1000)

    async def _ensure_loaded(self) -> None:
        if self.loaded:
            return
        async with self._load_lock:
            if self.loaded:
                return
            try:
                if self._backend is None:
                    self._backend = TorchScriptSileroBackend(model_path=self.model_path, device=self.device, speaker=self.speaker)
                await self._backend.load()
                self._loaded_at = datetime.utcnow()
                self._load_error = None
            except Exception as exc:
                self._load_error = self._sanitize(str(exc) or exc.__class__.__name__)
                self._last_error = self._load_error
                raise TTSConfigurationError(self._load_error) from exc

    def _sanitize(self, message: object) -> str:
        return sanitize_tts_error(message, self.model_path)


def _wav_from_float_samples(samples: list[float], sample_rate: int) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        for sample in samples:
            clipped = max(-1.0, min(1.0, float(sample)))
            writer.writeframesraw(int(clipped * 32767).to_bytes(2, byteorder="little", signed=True))
    return buffer.getvalue()
