from __future__ import annotations

import asyncio
import base64
import inspect
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Protocol

import httpx

from app.tts.base import TTSConfigurationError
from app.tts.local_engines.base import (
    LocalTTSEngine,
    LocalTTSSynthesisResult,
    audio_content_type,
    local_voice,
    sanitize_tts_error,
)


class KazakhTTS2Backend(Protocol):
    loaded: bool

    async def load(self) -> None:
        ...

    async def synthesize(self, text: str, language: str, voice: str | None, output_format: str | None) -> LocalTTSSynthesisResult:
        ...


@dataclass(frozen=True)
class KazakhTTS2BackendConfig:
    model_path: str = ""
    vocoder_path: str = ""
    tokenizer_path: str = ""
    server_url: str = ""
    server_timeout_seconds: float = 5.0
    device: str = "cuda"
    dtype: str = "auto"
    output_format: str = "wav"
    default_voice: str = ""


class HTTPKazakhTTS2Backend:
    def __init__(self, config: KazakhTTS2BackendConfig) -> None:
        self.config = config
        self.loaded = False

    async def load(self) -> None:
        self.loaded = True

    async def synthesize(self, text: str, language: str, voice: str | None, output_format: str | None) -> LocalTTSSynthesisResult:
        await self.load()
        requested_format = output_format or self.config.output_format or "wav"
        try:
            async with httpx.AsyncClient(timeout=self.config.server_timeout_seconds) as client:
                response = await client.post(
                    self.config.server_url,
                    json={
                        "text": text,
                        "language": language,
                        "voice": voice or self.config.default_voice or "",
                        "output_format": requested_format,
                    },
                )
                response.raise_for_status()
        except Exception as exc:
            raise TTSConfigurationError(f"KazakhTTS2 HTTP server error: {exc.__class__.__name__}") from exc

        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type.startswith("audio/"):
            return LocalTTSSynthesisResult(response.content, content_type, None)

        try:
            payload = response.json()
        except Exception as exc:
            raise TTSConfigurationError("KazakhTTS2 HTTP server returned a non-audio, non-JSON response.") from exc
        return _result_from_payload(payload, requested_format)


class LocalPythonKazakhTTS2Backend:
    """Lazy wrapper for an operator-installed KazakhTTS2 Python package.

    The repository does not vendor model code or weights. This wrapper looks for
    a locally installed package exposing either ``KazakhTTS2`` or ``load_model``.
    Missing packages or incompatible APIs fail as configuration errors without
    downloading anything.
    """

    def __init__(self, config: KazakhTTS2BackendConfig) -> None:
        self.config = config
        self.loaded = False
        self.module = None
        self.model = None

    async def load(self) -> None:
        if self.loaded:
            return
        await asyncio.to_thread(self._load_sync)

    async def synthesize(self, text: str, language: str, voice: str | None, output_format: str | None) -> LocalTTSSynthesisResult:
        await self.load()
        return await asyncio.to_thread(self._synthesize_sync, text, language, voice, output_format)

    def _load_sync(self) -> None:
        try:
            import importlib

            self.module = importlib.import_module("kazakh_tts2")
        except Exception as exc:
            raise TTSConfigurationError("Python package kazakh_tts2 is required for local KazakhTTS2 model loading.") from exc

        try:
            kwargs = {
                "model_path": self.config.model_path,
                "vocoder_path": self.config.vocoder_path,
                "tokenizer_path": self.config.tokenizer_path,
                "device": self.config.device,
                "dtype": self.config.dtype,
            }
            if hasattr(self.module, "KazakhTTS2"):
                self.model = self.module.KazakhTTS2(**kwargs)
            elif hasattr(self.module, "load_model"):
                self.model = self.module.load_model(**kwargs)
            else:
                raise TTSConfigurationError("kazakh_tts2 package must expose KazakhTTS2 or load_model.")
            if hasattr(self.model, "eval"):
                self.model.eval()
            self.loaded = True
        except Exception as exc:
            if isinstance(exc, TTSConfigurationError):
                raise
            raise TTSConfigurationError(str(exc)) from exc

    def _synthesize_sync(self, text: str, language: str, voice: str | None, output_format: str | None) -> LocalTTSSynthesisResult:
        if self.model is None and self.module is None:
            raise TTSConfigurationError("KazakhTTS2 model is not loaded.")
        requested_format = output_format or self.config.output_format or "wav"
        target = self.model if hasattr(self.model, "synthesize") else self.module
        if target is None or not hasattr(target, "synthesize"):
            raise TTSConfigurationError("KazakhTTS2 backend does not expose synthesize().")
        try:
            raw = target.synthesize(text=text, language=language, voice=voice, output_format=requested_format)
            if inspect.isawaitable(raw):
                raise TTSConfigurationError("KazakhTTS2 local synthesize() must be synchronous.")
            return _normalize_backend_result(raw, requested_format)
        except Exception as exc:
            if isinstance(exc, TTSConfigurationError):
                raise
            raise TTSConfigurationError(str(exc)) from exc


class KazakhTTS2Engine(LocalTTSEngine):
    name = "kazakh_tts2"

    def __init__(
        self,
        *,
        enabled: bool = False,
        model_path: str = "",
        vocoder_path: str = "",
        tokenizer_path: str = "",
        server_url: str = "",
        server_timeout_seconds: float = 5.0,
        device: str = "cuda",
        dtype: str = "auto",
        timeout_seconds: float = 5.0,
        load_on_startup: bool = False,
        output_format: str = "wav",
        default_voice: str = "",
        backend: KazakhTTS2Backend | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.model_path = model_path or ""
        self.vocoder_path = vocoder_path or ""
        self.tokenizer_path = tokenizer_path or ""
        self.server_url = server_url or ""
        self.server_timeout_seconds = float(server_timeout_seconds or timeout_seconds or 5.0)
        self.device = device or "cuda"
        self.dtype = dtype or "auto"
        self.timeout_seconds = float(timeout_seconds or 5.0)
        self.load_on_startup = bool(load_on_startup)
        self.output_format = output_format or "wav"
        self.default_voice = default_voice or ""
        self._backend = backend
        self._load_lock = asyncio.Lock()
        self._loaded_at: datetime | None = None
        self._last_error: str | None = None
        self._load_error: str | None = None
        self.request_count = 0
        self.error_count = 0
        self.timeout_count = 0
        self._latencies_ms: list[float] = []

    @property
    def loaded(self) -> bool:
        return bool(self._backend is not None and getattr(self._backend, "loaded", False))

    def supports(self, language: str) -> bool:
        return language == "kk" and bool(self.status_for_language(language).get("ready"))

    def default_voice_for_language(self, language: str) -> str:
        if language != "kk" or not self.enabled:
            return ""
        return self.default_voice or "kazakh_tts2-kk"

    def voice_catalog(self) -> dict[str, list[dict]]:
        voice_id = self.default_voice_for_language("kk")
        return {"kk": [local_voice("kk", self.name, voice_id)]} if voice_id and self.status()["ready"] else {}

    def status(self) -> dict:
        missing = self._missing_config()
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
            "supported_languages": ["kk"],
            "device": self.device,
            "dtype": self.dtype,
            "timeout_seconds": self.timeout_seconds,
            "server_timeout_seconds": self.server_timeout_seconds,
            "server_configured": bool(self.server_url),
            "loaded": self.loaded,
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
            "load_on_startup": self.load_on_startup,
            "output_format": self.output_format,
            "content_type": audio_content_type(self.output_format),
            "default_voice": self.default_voice_for_language("kk") if ready else "",
            "last_error": self._last_error or self._load_error,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "timeout_count": self.timeout_count,
            "average_latency_ms": self.average_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
        }

    def status_for_language(self, language: str) -> dict:
        status = self.status()
        if language != "kk" and self.enabled:
            status = dict(status)
            status["ready"] = False
            status["status"] = "not_configured"
            status["missing"] = ["KAZAKH_TTS2_SUPPORTS_KK_ONLY"]
            status["language"] = language
        else:
            status["language"] = language
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

    async def initialize(self) -> None:
        if self.load_on_startup:
            await self._ensure_backend_loaded()

    async def synthesize(self, text: str, language: str, voice: str | None = None, audio_format: str | None = None) -> LocalTTSSynthesisResult:
        status = self.status_for_language(language)
        if not status["ready"]:
            missing = ", ".join(status["missing"]) or status["status"]
            raise TTSConfigurationError(f"KazakhTTS2 is not configured: {missing}")
        started_at = perf_counter()
        requested_format = audio_format or self.output_format or "wav"
        selected_voice = voice or self.default_voice_for_language(language)
        try:
            backend = await asyncio.wait_for(self._ensure_backend_loaded(), timeout=self.timeout_seconds)
            result = await asyncio.wait_for(
                backend.synthesize(text, language, selected_voice, requested_format),
                timeout=self.timeout_seconds,
            )
            self._last_error = None
            return result
        except asyncio.TimeoutError as exc:
            self.timeout_count += 1
            self.error_count += 1
            self._last_error = self._sanitize(f"KazakhTTS2 timeout after {self.timeout_seconds:g}s")
            raise TTSConfigurationError(self._last_error) from exc
        except Exception as exc:
            self.error_count += 1
            self._last_error = self._sanitize(str(exc) or exc.__class__.__name__)
            if self._is_load_failure(exc):
                self._load_error = self._last_error
            raise TTSConfigurationError(self._last_error) from exc
        finally:
            self.request_count += 1
            self._latencies_ms.append((perf_counter() - started_at) * 1000)

    async def _ensure_backend_loaded(self) -> KazakhTTS2Backend:
        if self._backend is not None and getattr(self._backend, "loaded", False):
            return self._backend
        async with self._load_lock:
            if self._backend is not None and getattr(self._backend, "loaded", False):
                return self._backend
            if self._backend is None:
                self._backend = create_kazakh_tts2_backend(
                    KazakhTTS2BackendConfig(
                        model_path=self.model_path,
                        vocoder_path=self.vocoder_path,
                        tokenizer_path=self.tokenizer_path,
                        server_url=self.server_url,
                        server_timeout_seconds=self.server_timeout_seconds,
                        device=self.device,
                        dtype=self.dtype,
                        output_format=self.output_format,
                        default_voice=self.default_voice,
                    )
                )
            try:
                await self._backend.load()
                self._loaded_at = datetime.utcnow()
                self._load_error = None
            except Exception as exc:
                self._load_error = self._sanitize(str(exc) or exc.__class__.__name__)
                self._last_error = self._load_error
                raise TTSConfigurationError(self._load_error) from exc
            return self._backend

    def _missing_config(self) -> list[str]:
        if not self.enabled:
            return []
        if self.server_url:
            return []
        missing = []
        if not self.model_path:
            missing.append("KAZAKH_TTS2_MODEL_PATH")
        if not self.vocoder_path:
            missing.append("KAZAKH_TTS2_VOCODER_PATH")
        if not self.tokenizer_path:
            missing.append("KAZAKH_TTS2_TOKENIZER_PATH")
        if missing:
            missing.append("KAZAKH_TTS2_SERVER_URL")
        return missing

    def _sanitize(self, message: object) -> str:
        return sanitize_tts_error(message, self.model_path, self.vocoder_path, self.tokenizer_path, self.server_url)

    @staticmethod
    def _is_load_failure(exc: Exception) -> bool:
        return "load" in exc.__class__.__name__.lower() or "required for local KazakhTTS2 model loading" in str(exc)


def create_kazakh_tts2_backend(config: KazakhTTS2BackendConfig) -> KazakhTTS2Backend:
    if config.server_url:
        return HTTPKazakhTTS2Backend(config)
    return LocalPythonKazakhTTS2Backend(config)


def _result_from_payload(payload: object, output_format: str | None) -> LocalTTSSynthesisResult:
    if not isinstance(payload, dict):
        raise TTSConfigurationError("KazakhTTS2 HTTP server returned a non-JSON-object response.")
    encoded = None
    for key in ("audio_base64", "audio", "audio_bytes"):
        value = payload.get(key)
        if isinstance(value, str):
            encoded = value
            break
    if not encoded:
        raise TTSConfigurationError("KazakhTTS2 HTTP server JSON did not include base64 audio.")
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    try:
        audio = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise TTSConfigurationError("KazakhTTS2 HTTP server returned invalid base64 audio.") from exc
    content_type = str(payload.get("mime_type") or payload.get("content_type") or audio_content_type(output_format))
    duration = payload.get("duration_ms")
    return LocalTTSSynthesisResult(audio, content_type, int(duration) if isinstance(duration, int | float) else None)


def _normalize_backend_result(raw: object, output_format: str | None) -> LocalTTSSynthesisResult:
    if isinstance(raw, LocalTTSSynthesisResult):
        return raw
    if isinstance(raw, bytes):
        return LocalTTSSynthesisResult(raw, audio_content_type(output_format), None)
    if isinstance(raw, tuple):
        audio = raw[0]
        if not isinstance(audio, bytes):
            raise TTSConfigurationError("KazakhTTS2 backend tuple did not include audio bytes.")
        content_type = raw[1] if len(raw) > 1 and raw[1] else audio_content_type(output_format)
        duration = raw[2] if len(raw) > 2 else None
        return LocalTTSSynthesisResult(audio, str(content_type), int(duration) if isinstance(duration, int | float) else None)
    if isinstance(raw, dict):
        audio = raw.get("audio_bytes")
        if isinstance(audio, bytes):
            return LocalTTSSynthesisResult(
                audio,
                str(raw.get("mime_type") or raw.get("content_type") or audio_content_type(output_format)),
                int(raw["duration_ms"]) if isinstance(raw.get("duration_ms"), int | float) else None,
            )
        return _result_from_payload(raw, output_format)
    raise TTSConfigurationError("KazakhTTS2 backend returned an unsupported result type.")
