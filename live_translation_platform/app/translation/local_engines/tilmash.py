from __future__ import annotations

import asyncio
from datetime import datetime
from time import perf_counter

from app.translation.local_engines.base import LocalTranslationConfigurationError, LocalTranslationEngine, normalize_translation_language
from app.translation.local_engines.model_loader import (
    LocalModelInferenceError,
    LocalModelLoadError,
    LocalTranslationBackend,
    TilmashBackendConfig,
    create_tilmash_backend,
    sanitize_error_message,
    _plain_translation_text,
)


class TilmashTranslationEngine(LocalTranslationEngine):
    name = "tilmash"
    supported_targets = {"kk", "uz"}
    model_path_env = "TILMASH_MODEL_PATH"

    def __init__(
        self,
        *,
        enabled: bool = True,
        model_path: str = "",
        tokenizer_path: str = "",
        server_url: str = "",
        server_timeout_seconds: float = 1.5,
        device: str = "cuda",
        dtype: str = "auto",
        max_batch_size: int = 8,
        max_new_tokens: int = 128,
        num_beams: int = 1,
        timeout_seconds: float = 1.5,
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
        self.server_timeout_seconds = float(server_timeout_seconds or timeout_seconds or 1.5)
        self.dtype = dtype or "auto"
        self.max_batch_size = max(1, int(max_batch_size or 1))
        self.max_new_tokens = max(1, int(max_new_tokens or 128))
        self.num_beams = max(1, int(num_beams or 1))
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
        self._unsupported_targets: set[str] = set()

    @property
    def loaded(self) -> bool:
        return bool(self._backend is not None and getattr(self._backend, "loaded", False))

    def status(self) -> dict:
        missing = []
        if self.enabled and not self.model_path and not self.server_url:
            missing.append(self.model_path_env)
        configured = self.enabled and not missing
        ready = configured and self._load_error is None
        if not self.enabled:
            status_value = "disabled"
        elif missing:
            status_value = "not_configured"
        elif self._load_error:
            status_value = "error"
        elif self.loaded:
            status_value = "loaded"
        else:
            status_value = "configured"
        status = {
            "ready": ready,
            "status": status_value,
            "missing": missing,
            "enabled": self.enabled,
            "engine": self.name,
            "supported_source_languages": ["ru"],
            "supported_target_languages": sorted(self.supported_targets),
            "supported_language_pairs": ["ru->kk", "ru->uz"],
            "device": self.device,
            "loaded": self.loaded,
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
            "last_error": self._last_error,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "timeout_count": self.timeout_count,
            "average_latency_ms": self.average_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "dtype": self.dtype,
            "load_on_startup": self.load_on_startup,
            "server_configured": bool(self.server_url),
        }
        status["max_batch_size"] = self.max_batch_size
        status["max_new_tokens"] = self.max_new_tokens
        status["num_beams"] = self.num_beams
        status["unsupported_target_languages"] = sorted(self._unsupported_targets)
        language_status = self._language_status()
        if language_status:
            status["tokenizer_language_status"] = language_status
            unsupported = set(status["unsupported_target_languages"])
            unsupported.update(language_status.get("unsupported_project_languages", []))
            status["unsupported_target_languages"] = sorted(unsupported)
        return status

    async def translate(self, text: str, source_language: str, target_language: str) -> str:
        return (await self.translate_batch([text], source_language, target_language))[0]

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        self._ensure_ready()
        normalized_target = normalize_translation_language(target_language)
        if not self.supports(source_language, normalized_target):
            raise LocalTranslationConfigurationError(f"tilmash does not support {source_language} -> {target_language}")
        batches = [texts[index : index + self.max_batch_size] for index in range(0, len(texts), self.max_batch_size)]
        results: list[str] = []
        for batch in batches:
            results.extend(await self._translate_batch_once(batch, source_language, normalized_target))
        return results

    async def initialize(self) -> None:
        await self._ensure_backend_loaded()

    def record_timeout(self, message: str) -> None:
        self.timeout_count += 1
        self.error_count += 1
        self._last_error = self._sanitize(message)

    @property
    def average_latency_ms(self) -> float:
        if not self._latencies_ms:
            return 0.0
        return sum(self._latencies_ms) / len(self._latencies_ms)

    @property
    def p95_latency_ms(self) -> float | None:
        if not self._latencies_ms:
            return None
        values = sorted(self._latencies_ms)
        index = max(0, min(len(values) - 1, int(round(0.95 * (len(values) - 1)))))
        return values[index]

    def _ensure_ready(self) -> None:
        if not self.enabled:
            raise LocalTranslationConfigurationError("tilmash is disabled: TILMASH_ENABLED=false")
        if not self.model_path and not self.server_url:
            raise LocalTranslationConfigurationError("tilmash is not configured: TILMASH_MODEL_PATH")
        if self._load_error:
            raise LocalTranslationConfigurationError(f"tilmash model load failed: {self._load_error}")

    async def _translate_batch_once(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        started_at = perf_counter()
        self.request_count += len(texts)
        try:
            backend = await self._ensure_backend_loaded()
            raw = await backend.translate_batch(texts, source_language, target_language)
            self._last_error = None
            self._unsupported_targets.discard(target_language)
            return [_plain_translation_text(item) for item in raw]
        except (LocalModelLoadError, LocalModelInferenceError, RuntimeError) as exc:
            self.error_count += 1
            self._last_error = self._sanitize(exc)
            if "unsupported" in self._last_error.lower():
                self._unsupported_targets.add(target_language)
            if isinstance(exc, LocalModelLoadError):
                self._load_error = self._last_error
            raise LocalTranslationConfigurationError(self._last_error) from exc
        finally:
            self._latencies_ms.append((perf_counter() - started_at) * 1000)

    async def _ensure_backend_loaded(self) -> LocalTranslationBackend:
        async with self._load_lock:
            if self._backend is None:
                self._backend = create_tilmash_backend(
                    TilmashBackendConfig(
                        model_path=self.model_path,
                        tokenizer_path=self.tokenizer_path,
                        server_url=self.server_url,
                        server_timeout_seconds=self.server_timeout_seconds,
                        device=self.device,
                        dtype=self.dtype,
                        max_batch_size=self.max_batch_size,
                        max_new_tokens=self.max_new_tokens,
                        num_beams=self.num_beams,
                    )
                )
            if not getattr(self._backend, "loaded", False):
                try:
                    await self._backend.load()
                    self._loaded_at = datetime.utcnow()
                    self._load_error = None
                except Exception as exc:
                    self._load_error = self._sanitize(exc)
                    self._last_error = self._load_error
                    raise LocalModelLoadError(self._load_error) from exc
            return self._backend

    def _sanitize(self, message) -> str:
        return sanitize_error_message(message, self.model_path, self.tokenizer_path, self.server_url)

    def _language_status(self) -> dict:
        backend = self._backend
        if backend is None or not hasattr(backend, "language_status"):
            return {}
        try:
            status = backend.language_status()
        except Exception as exc:
            return {"error": self._sanitize(exc)}
        return status if isinstance(status, dict) else {}
