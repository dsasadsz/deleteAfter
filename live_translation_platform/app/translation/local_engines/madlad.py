from __future__ import annotations

import asyncio
from datetime import datetime
from time import perf_counter

from app.translation.local_engines.base import LocalTranslationConfigurationError, LocalTranslationEngine, normalize_translation_language
from app.translation.local_engines.model_loader import (
    LocalModelInferenceError,
    LocalModelLoadError,
    LocalTranslationBackend,
    MadladBackendConfig,
    create_madlad_backend,
    sanitize_error_message,
    _plain_translation_text,
)


class MadladTranslationEngine(LocalTranslationEngine):
    name = "madlad400"
    supported_targets = {"zh-Hans"}
    model_path_env = "MADLAD_MODEL_PATH"

    def __init__(
        self,
        *,
        enabled: bool = False,
        model_path: str = "",
        tokenizer_path: str = "",
        server_url: str = "",
        server_timeout_seconds: float = 4.0,
        device: str = "cuda",
        dtype: str = "auto",
        quantization: str = "8bit",
        max_batch_size: int = 4,
        timeout_seconds: float = 4.0,
        load_on_startup: bool = False,
        backend: LocalTranslationBackend | None = None,
    ) -> None:
        super().__init__(
            enabled=enabled,
            model_path=model_path,
            device=device,
            timeout_seconds=timeout_seconds,
        )
        self.tokenizer_path = tokenizer_path or ""
        self.server_url = server_url or ""
        self.server_timeout_seconds = float(server_timeout_seconds or timeout_seconds or 4.0)
        self.dtype = dtype or "auto"
        self.quantization = quantization or "8bit"
        self.max_batch_size = max(1, int(max_batch_size or 4))
        self.load_on_startup = bool(load_on_startup)
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

    def status(self) -> dict:
        missing = []
        if self.enabled and not (self.model_path or self.server_url):
            missing.append(self.model_path_env)
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
            "missing": missing,
            "enabled": self.enabled,
            "engine": self.name,
            "supported_source_languages": ["ru"],
            "supported_target_languages": sorted(self.supported_targets),
            "supported_language_pairs": [f"ru->{language}" for language in sorted(self.supported_targets)],
            "device": self.device,
            "loaded": self.loaded,
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
            "last_error": self._last_error or self._load_error,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "timeout_count": self.timeout_count,
            "average_latency_ms": self.average_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "dtype": self.dtype,
            "quantization": self.quantization,
            "load_on_startup": self.load_on_startup,
            "server_configured": bool(self.server_url),
            "max_batch_size": self.max_batch_size,
        }

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
        if not self.load_on_startup:
            return
        await self._ensure_backend_loaded()

    async def translate(self, text: str, source_language: str, target_language: str) -> str:
        translations = await self.translate_batch([text], source_language, target_language)
        return translations[0]

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        self._ensure_ready()
        source = normalize_translation_language(source_language)
        target = normalize_translation_language(target_language)
        if source != "ru" or target not in self.supported_targets:
            raise LocalTranslationConfigurationError(f"{self.name} does not support {source_language} -> {target_language}")

        results: list[str] = []
        for start in range(0, len(texts), self.max_batch_size):
            batch = texts[start : start + self.max_batch_size]
            results.extend(await self._translate_batch_once(batch, source_language, target))
        return results

    def record_timeout(self, message: str) -> None:
        self.timeout_count += 1
        self.error_count += 1
        self._last_error = self._sanitize(message)

    def _ensure_ready(self) -> None:
        status = self.status()
        if not status["ready"]:
            missing = ", ".join(status["missing"]) or status["status"]
            raise LocalTranslationConfigurationError(f"{self.name} is not configured: {missing}")

    async def _translate_batch_once(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        started_at = perf_counter()
        try:
            await self._ensure_backend_loaded()
            if self._backend is None:
                raise LocalTranslationConfigurationError("MADLAD backend is not configured.")
            translations = await self._backend.translate_batch(texts, source_language, target_language)
            if len(translations) != len(texts):
                raise LocalModelInferenceError("MADLAD backend returned an unexpected translation count.")
            return [_plain_translation_text(item) for item in translations]
        except (LocalModelLoadError, LocalModelInferenceError, LocalTranslationConfigurationError) as exc:
            self.error_count += 1
            self._last_error = self._sanitize(str(exc))
            raise LocalTranslationConfigurationError(self._last_error) from exc
        except Exception as exc:
            self.error_count += 1
            self._last_error = self._sanitize(str(exc) or exc.__class__.__name__)
            raise LocalTranslationConfigurationError(self._last_error) from exc
        finally:
            self.request_count += 1
            self._latencies_ms.append((perf_counter() - started_at) * 1000)

    async def _ensure_backend_loaded(self) -> None:
        if self._backend is not None and getattr(self._backend, "loaded", False):
            return
        async with self._load_lock:
            if self._backend is not None and getattr(self._backend, "loaded", False):
                return
            try:
                if self._backend is None:
                    self._backend = create_madlad_backend(
                        MadladBackendConfig(
                            model_path=self.model_path,
                            tokenizer_path=self.tokenizer_path,
                            server_url=self.server_url,
                            server_timeout_seconds=self.server_timeout_seconds,
                            device=self.device,
                            dtype=self.dtype,
                            quantization=self.quantization,
                            max_batch_size=self.max_batch_size,
                        )
                    )
                await self._backend.load()
                self._loaded_at = datetime.utcnow()
                self._load_error = None
            except Exception as exc:
                self._load_error = self._sanitize(str(exc) or exc.__class__.__name__)
                self._last_error = self._load_error
                raise LocalTranslationConfigurationError(self._load_error) from exc

    def _sanitize(self, message: object) -> str:
        return sanitize_error_message(message, self.model_path, self.tokenizer_path, self.server_url)
