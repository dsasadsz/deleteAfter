from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Protocol

from app.translation.local_engines.base import (
    LocalTranslationConfigurationError,
    LocalTranslationEngine,
    normalize_translation_language,
)
from app.translation.local_engines.model_loader import (
    LocalModelInferenceError,
    LocalModelLoadError,
    sanitize_error_message,
    _plain_translation_text,
)


PROJECT_TO_M2M100_LANG = {
    "ru": "ru",
    "ru-RU": "ru",
    "uz": "uz",
    "uz-UZ": "uz",
    "zh": "zh",
    "zh-CN": "zh",
    "zh-Hans": "zh",
}

_PROJECT_TO_M2M100_LANG_NORMALIZED = {key.lower().replace("_", "-"): value for key, value in PROJECT_TO_M2M100_LANG.items()}


class M2M100Ct2Backend(Protocol):
    loaded: bool

    async def load(self) -> None:
        ...

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        ...


@dataclass(frozen=True)
class M2M100Ct2BackendConfig:
    model_path: str
    tokenizer_path: str
    device: str = "cpu"
    compute_type: str = "int8"


class CTranslate2M2M100Backend:
    def __init__(self, config: M2M100Ct2BackendConfig) -> None:
        self.config = config
        self.loaded = False
        self.tokenizer = None
        self.translator = None

    async def load(self) -> None:
        if self.loaded:
            return
        await asyncio.to_thread(self._load_sync)

    async def translate_batch(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        await self.load()
        return await asyncio.to_thread(self._translate_batch_sync, texts, source_language, target_language)

    def _load_sync(self) -> None:
        try:
            import ctranslate2
        except Exception as exc:
            raise LocalModelLoadError("Python package ctranslate2 is required for M2M100 CT2 translation.") from exc
        try:
            from transformers import M2M100Tokenizer
        except Exception as exc:
            raise LocalModelLoadError("Python package transformers is required for M2M100 tokenizer loading.") from exc
        try:
            self.tokenizer = M2M100Tokenizer.from_pretrained(self.config.tokenizer_path, local_files_only=True)
            self.translator = ctranslate2.Translator(
                self.config.model_path,
                device=self.config.device,
                compute_type=self.config.compute_type,
            )
            self.loaded = True
        except Exception as exc:
            raise LocalModelLoadError(str(exc)) from exc

    def _translate_batch_sync(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        if self.tokenizer is None or self.translator is None:
            raise LocalModelInferenceError("M2M100 CT2 backend is not loaded.")
        source_code = resolve_m2m100_language_code(source_language)
        target_code = resolve_m2m100_language_code(target_language)
        try:
            self.tokenizer.src_lang = source_code
            target_prefix_token = _m2m100_language_token(self.tokenizer, target_code)
            source_tokens = [self.tokenizer.convert_ids_to_tokens(self.tokenizer.encode(text)) for text in texts]
            # M2M100Tokenizer uses src_lang to add the source language token during encoding.
            # CT2 converted seq2seq models expect token strings, so the decoder target is
            # constrained with the tokenizer's concrete target language token (for example
            # "__zh__" when exposed by lang_code_to_token). We strip that prefix before
            # decoding so callers receive plain translated text only.
            results = self.translator.translate_batch(
                source_tokens,
                target_prefix=[[target_prefix_token] for _ in source_tokens],
            )
            outputs: list[str] = []
            for result in results:
                hypothesis = list(result.hypotheses[0])
                if hypothesis and hypothesis[0] == target_prefix_token:
                    hypothesis = hypothesis[1:]
                token_ids = self.tokenizer.convert_tokens_to_ids(hypothesis)
                outputs.append(_plain_translation_text(self.tokenizer.decode(token_ids, skip_special_tokens=True)))
            return outputs
        except Exception as exc:
            raise LocalModelInferenceError(str(exc)) from exc


def create_m2m100_ct2_backend(config: M2M100Ct2BackendConfig) -> M2M100Ct2Backend:
    return CTranslate2M2M100Backend(config)


class M2M100Ct2TranslationEngine(LocalTranslationEngine):
    name = "m2m100_ct2"
    model_path_env = "M2M100_CT2_MODEL_PATH"

    def __init__(
        self,
        *,
        enabled: bool = False,
        model_path: str = "",
        tokenizer_path: str = "",
        device: str = "cpu",
        compute_type: str = "int8",
        timeout_seconds: float = 5.0,
        load_on_startup: bool = False,
        default_size: str = "418m",
        supported_targets: str | list[str] | set[str] = "uz,zh-Hans",
        name: str | None = None,
        model_path_env: str | None = None,
        tokenizer_path_env: str = "M2M100_CT2_TOKENIZER_PATH",
        backend: M2M100Ct2Backend | None = None,
    ) -> None:
        super().__init__(enabled=enabled, model_path=model_path, device=device, timeout_seconds=timeout_seconds)
        if name:
            self.name = name
        if model_path_env:
            self.model_path_env = model_path_env
        self.tokenizer_path_env = tokenizer_path_env
        self.tokenizer_path = tokenizer_path or ""
        self.compute_type = compute_type or "int8"
        self.load_on_startup = bool(load_on_startup)
        self.default_size = default_size or "418m"
        self.supported_targets = _supported_targets_from_config(supported_targets)
        self._backend = backend
        self._load_lock = asyncio.Lock()
        self._last_error: str | None = None
        self._load_error: str | None = None
        self._loaded_at: datetime | None = None
        self.request_count = 0
        self.error_count = 0
        self.timeout_count = 0
        self.fallback_count = 0
        self._latencies_ms: list[float] = []

    @property
    def loaded(self) -> bool:
        return bool(self._backend is not None and getattr(self._backend, "loaded", False))

    def supports(self, source_language: str, target_language: str) -> bool:
        source = normalize_translation_language(source_language)
        try:
            target = normalize_m2m100_target_language(target_language)
        except LocalTranslationConfigurationError:
            return False
        return self.enabled and source == "ru" and target in self.supported_targets

    def status(self) -> dict:
        missing = []
        if self.enabled and not self.model_path:
            missing.append(self.model_path_env)
        if self.enabled and not self.tokenizer_path:
            missing.append(self.tokenizer_path_env)
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
        ready = self.enabled and not missing and not self._load_error
        return {
            "ready": ready,
            "status": status_value,
            "missing": missing,
            "enabled": self.enabled,
            "engine": self.name,
            "supported_source_languages": ["ru"],
            "supported_target_languages": sorted(self.supported_targets),
            "supported_language_pairs": [f"ru->{language}" for language in sorted(self.supported_targets)],
            "device": self.device,
            "compute_type": self.compute_type,
            "model_size": self.default_size,
            "default_size": self.default_size,
            "loaded": self.loaded,
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
            "load_on_startup": self.load_on_startup,
            "last_error": self._last_error or self._load_error,
            "request_count": self.request_count,
            "error_count": self.error_count,
            "timeout_count": self.timeout_count,
            "fallback_count": self.fallback_count,
            "average_latency_ms": self.average_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
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
        target = normalize_m2m100_target_language(target_language)
        if source != "ru" or target not in self.supported_targets:
            raise LocalTranslationConfigurationError(f"{self.name} does not support {source_language} -> {target_language}")
        return await self._translate_batch_once(texts, source_language, target)

    def record_timeout(self, message: str) -> None:
        self.timeout_count += 1
        self.error_count += 1
        self._last_error = self._sanitize(message)

    def record_fallback(self) -> None:
        self.fallback_count += 1

    def _ensure_ready(self) -> None:
        status = self.status()
        if not status["ready"]:
            missing = ", ".join(status["missing"]) or status["status"]
            raise LocalTranslationConfigurationError(f"{self.name} is not configured: {missing}")

    async def _translate_batch_once(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        started_at = perf_counter()
        try:
            backend = await self._ensure_backend_loaded()
            translations = await backend.translate_batch(texts, source_language, target_language)
            if len(translations) != len(texts):
                raise LocalModelInferenceError("M2M100 CT2 backend returned an unexpected translation count.")
            self._last_error = None
            return [_plain_translation_text(item) for item in translations]
        except (LocalModelLoadError, LocalModelInferenceError, LocalTranslationConfigurationError) as exc:
            self.error_count += 1
            self._last_error = self._sanitize(str(exc) or exc.__class__.__name__)
            if isinstance(exc, LocalModelLoadError):
                self._load_error = self._last_error
            raise LocalTranslationConfigurationError(self._last_error) from exc
        except Exception as exc:
            self.error_count += 1
            self._last_error = self._sanitize(str(exc) or exc.__class__.__name__)
            raise LocalTranslationConfigurationError(self._last_error) from exc
        finally:
            self.request_count += len(texts)
            self._latencies_ms.append((perf_counter() - started_at) * 1000)

    async def _ensure_backend_loaded(self) -> M2M100Ct2Backend:
        if self._backend is not None and getattr(self._backend, "loaded", False):
            return self._backend
        async with self._load_lock:
            if self._backend is None:
                self._backend = create_m2m100_ct2_backend(
                    M2M100Ct2BackendConfig(
                        model_path=self.model_path,
                        tokenizer_path=self.tokenizer_path,
                        device=self.device,
                        compute_type=self.compute_type,
                    )
                )
            if not getattr(self._backend, "loaded", False):
                try:
                    await self._backend.load()
                    self._loaded_at = datetime.utcnow()
                    self._load_error = None
                except Exception as exc:
                    self._load_error = self._sanitize(str(exc) or exc.__class__.__name__)
                    self._last_error = self._load_error
                    raise LocalModelLoadError(self._load_error) from exc
            return self._backend

    def _sanitize(self, message: object) -> str:
        return sanitize_error_message(message, self.model_path, self.tokenizer_path)


def resolve_m2m100_language_code(project_language: str) -> str:
    normalized = (project_language or "").strip().replace("_", "-")
    mapped = PROJECT_TO_M2M100_LANG.get(normalized) or _PROJECT_TO_M2M100_LANG_NORMALIZED.get(normalized.lower())
    if not mapped:
        raise LocalTranslationConfigurationError(f"M2M100 language is unsupported: {project_language}")
    return mapped


def normalize_m2m100_target_language(project_language: str) -> str:
    code = resolve_m2m100_language_code(project_language)
    if code == "uz":
        return "uz"
    if code == "zh":
        return "zh-Hans"
    raise LocalTranslationConfigurationError(f"M2M100 target language is unsupported: {project_language}")


def _supported_targets_from_config(value: str | list[str] | set[str]) -> set[str]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    else:
        items = [str(item).strip() for item in value if str(item).strip()]
    targets: set[str] = set()
    for item in items:
        try:
            targets.add(normalize_m2m100_target_language(item))
        except LocalTranslationConfigurationError:
            continue
    return targets or {"uz", "zh-Hans"}


def _m2m100_language_token(tokenizer, target_code: str) -> str:
    mapping = getattr(tokenizer, "lang_code_to_token", None)
    if isinstance(mapping, dict) and mapping.get(target_code):
        return str(mapping[target_code])
    converter = getattr(tokenizer, "get_lang_id", None)
    if callable(converter):
        token_id = converter(target_code)
        token = tokenizer.convert_ids_to_tokens(token_id)
        if isinstance(token, str):
            return token
    return target_code
