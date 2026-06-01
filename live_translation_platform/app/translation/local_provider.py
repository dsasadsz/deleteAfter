from __future__ import annotations

import asyncio
from datetime import datetime
from time import perf_counter
from typing import Any

from app.translation.base import TranslationProvider
from app.translation.local_engines.base import LocalTranslationConfigurationError, LocalTranslationTimeoutError, normalize_translation_language
from app.translation.local_engines.m2m100_ct2 import M2M100Ct2TranslationEngine
from app.translation.local_engines.model_loader import sanitize_error_message
from app.translation.local_engines.madlad import MadladTranslationEngine
from app.translation.local_engines.tilmash import TilmashTranslationEngine


DISABLED_TRANSLATION_ROUTE = "disabled"
LOCAL_TRANSLATION_STATUS_LANGUAGES = ("kk", "zh-Hans", "uz")


class LocalTranslationProvider(TranslationProvider):
    name = "local"

    def __init__(
        self,
        *,
        enabled: bool = False,
        routing_enabled: bool = True,
        default_engine: str = "tilmash",
        fallback_engine: str = "madlad400",
        route_table: dict[str, str] | None = None,
        timeout_seconds: float = 2.5,
        engines: dict[str, Any] | None = None,
        fallback_provider: TranslationProvider | None = None,
        tilmash_enabled: bool = True,
        tilmash_model_path: str = "",
        tilmash_tokenizer_path: str = "",
        tilmash_server_url: str = "",
        tilmash_server_timeout_seconds: float = 1.5,
        tilmash_device: str = "cuda",
        tilmash_dtype: str = "auto",
        tilmash_max_batch_size: int = 8,
        tilmash_max_new_tokens: int = 128,
        tilmash_num_beams: int = 1,
        tilmash_timeout_seconds: float = 1.5,
        tilmash_load_on_startup: bool = False,
        madlad_enabled: bool = False,
        madlad_model_path: str = "",
        madlad_tokenizer_path: str = "",
        madlad_server_url: str = "",
        madlad_server_timeout_seconds: float = 4.0,
        madlad_device: str = "cuda",
        madlad_dtype: str = "auto",
        madlad_quantization: str = "8bit",
        madlad_max_batch_size: int = 4,
        madlad_timeout_seconds: float = 4.0,
        madlad_load_on_startup: bool = False,
        m2m100_ct2_enabled: bool = False,
        m2m100_ct2_model_path: str = "",
        m2m100_ct2_tokenizer_path: str = "",
        m2m100_ct2_device: str = "cpu",
        m2m100_ct2_compute_type: str = "int8",
        m2m100_ct2_timeout_seconds: float = 5.0,
        m2m100_ct2_load_on_startup: bool = False,
        m2m100_ct2_default_size: str = "418m",
        m2m100_ct2_supported_targets: str = "uz,zh-Hans",
        m2m100_1_2b_ct2_enabled: bool = False,
        m2m100_1_2b_ct2_model_path: str = "",
        m2m100_1_2b_ct2_tokenizer_path: str = "",
        m2m100_1_2b_ct2_device: str = "cpu",
        m2m100_1_2b_ct2_compute_type: str = "int8",
        m2m100_1_2b_ct2_timeout_seconds: float = 8.0,
        m2m100_1_2b_ct2_load_on_startup: bool = False,
        m2m100_1_2b_ct2_supported_targets: str = "uz",
    ) -> None:
        self.enabled = bool(enabled)
        self.routing_enabled = bool(routing_enabled)
        self.default_engine = (default_engine or "tilmash").lower()
        self.fallback_engine = (fallback_engine or "").lower()
        self.route_table = _normalize_route_table(route_table)
        self.timeout_seconds = float(timeout_seconds or 2.5)
        self.engines = engines or {
            "tilmash": TilmashTranslationEngine(
                enabled=tilmash_enabled,
                model_path=tilmash_model_path,
                tokenizer_path=tilmash_tokenizer_path,
                server_url=tilmash_server_url,
                server_timeout_seconds=tilmash_server_timeout_seconds,
                device=tilmash_device,
                dtype=tilmash_dtype,
                max_batch_size=tilmash_max_batch_size,
                max_new_tokens=tilmash_max_new_tokens,
                num_beams=tilmash_num_beams,
                timeout_seconds=tilmash_timeout_seconds,
                load_on_startup=tilmash_load_on_startup,
            ),
            "madlad400": MadladTranslationEngine(
                enabled=madlad_enabled,
                model_path=madlad_model_path,
                tokenizer_path=madlad_tokenizer_path,
                server_url=madlad_server_url,
                server_timeout_seconds=madlad_server_timeout_seconds,
                device=madlad_device,
                dtype=madlad_dtype,
                quantization=madlad_quantization,
                max_batch_size=madlad_max_batch_size,
                timeout_seconds=madlad_timeout_seconds,
                load_on_startup=madlad_load_on_startup,
            ),
            "m2m100_ct2": M2M100Ct2TranslationEngine(
                enabled=m2m100_ct2_enabled,
                model_path=m2m100_ct2_model_path,
                tokenizer_path=m2m100_ct2_tokenizer_path,
                device=m2m100_ct2_device,
                compute_type=m2m100_ct2_compute_type,
                timeout_seconds=m2m100_ct2_timeout_seconds,
                load_on_startup=m2m100_ct2_load_on_startup,
                default_size=m2m100_ct2_default_size,
                supported_targets=m2m100_ct2_supported_targets,
            ),
            "m2m100_1_2b_ct2": M2M100Ct2TranslationEngine(
                enabled=m2m100_1_2b_ct2_enabled,
                model_path=m2m100_1_2b_ct2_model_path,
                tokenizer_path=m2m100_1_2b_ct2_tokenizer_path,
                device=m2m100_1_2b_ct2_device,
                compute_type=m2m100_1_2b_ct2_compute_type,
                timeout_seconds=m2m100_1_2b_ct2_timeout_seconds,
                load_on_startup=m2m100_1_2b_ct2_load_on_startup,
                default_size="1.2b",
                supported_targets=m2m100_1_2b_ct2_supported_targets,
                name="m2m100_1_2b_ct2",
                model_path_env="M2M100_1_2B_CT2_MODEL_PATH",
                tokenizer_path_env="M2M100_1_2B_CT2_TOKENIZER_PATH",
            ),
        }
        self.fallback_provider = fallback_provider or _fallback_provider_for_engine(self.fallback_engine)
        self.translation_requests_count = 0
        self.translation_errors_count = 0
        self.translation_fallbacks_count = 0
        self.translation_timeouts_count = 0
        self.translation_last_error: str | None = None
        self.translation_last_success_at: datetime | None = None
        self.translation_avg_latency_ms = 0.0
        self._latencies_ms: list[float] = []

    async def translate_many(
        self,
        text: str,
        source_language: str,
        target_languages: list[str],
    ) -> dict[str, str]:
        self.translation_requests_count += 1
        started_at = perf_counter()
        translations: dict[str, str] = {}
        try:
            for target_language in target_languages:
                target = normalize_translation_language(target_language)
                translations[target] = await self._translate_one(text, source_language, target)
            self.translation_last_success_at = datetime.utcnow()
            return translations
        finally:
            latency_ms = (perf_counter() - started_at) * 1000
            self._latencies_ms.append(latency_ms)
            self.translation_avg_latency_ms = sum(self._latencies_ms) / len(self._latencies_ms)

    @property
    def translation_p95_latency_ms(self) -> float | None:
        if not self._latencies_ms:
            return None
        values = sorted(self._latencies_ms)
        index = max(0, min(len(values) - 1, int(round(0.95 * (len(values) - 1)))))
        return values[index]

    def status(self) -> dict:
        engine_statuses = {
            name: engine.status() if hasattr(engine, "status") else {"ready": True, "status": "ready", "missing": []}
            for name, engine in self.engines.items()
        }
        missing = []
        if not self.enabled:
            missing.append("LOCAL_TRANSLATION_ENABLED")
        for status in engine_statuses.values():
            missing.extend(status.get("missing", []))
        fallback_engine_status = engine_statuses.get(self.fallback_engine, {})
        fallback_available = self.fallback_provider is not None or bool(fallback_engine_status.get("ready"))
        ready = self.enabled and (
            any(bool(status.get("ready")) for status in engine_statuses.values())
            or (fallback_available and self.translation_fallbacks_count > 0)
        )
        degraded = self.translation_fallbacks_count > 0 or self.translation_errors_count > 0
        status_value = "degraded" if ready and degraded else "ready" if ready else "not_configured"
        return {
            "ready": ready,
            "status": status_value,
            "missing": missing,
            "enabled": self.enabled,
            "routing_enabled": self.routing_enabled,
            "default_engine": self.default_engine,
            "fallback_engine": self.fallback_engine,
            "route_table": dict(self.route_table),
            "route_status_by_language": self._route_status_by_language(engine_statuses),
            "engines": engine_statuses,
            "metrics": {
                "request_count": self.translation_requests_count,
                "error_count": self.translation_errors_count,
                "fallback_count": self.translation_fallbacks_count,
                "timeout_count": self.translation_timeouts_count,
                "average_latency_ms": self.translation_avg_latency_ms,
                "p95_latency_ms": self.translation_p95_latency_ms,
                "last_error": self.translation_last_error,
            },
        }

    async def initialize(self) -> None:
        for engine in self.engines.values():
            if not bool(getattr(engine, "load_on_startup", False)):
                continue
            try:
                if hasattr(engine, "initialize"):
                    await engine.initialize()
            except Exception as exc:
                self._record_error(exc)
                raise

    async def _translate_one(self, text: str, source_language: str, target_language: str) -> str:
        target = normalize_translation_language(target_language)
        if self._route_is_disabled(target):
            return _disabled_translation_message(target)
        engine = self._engine_for_target(source_language, target_language)
        try:
            if engine is None:
                raise LocalTranslationConfigurationError(f"No local translation engine configured for {source_language} -> {target_language}")
            return await self._translate_with_timeout(engine, text, source_language, target_language)
        except Exception as exc:
            self._record_error(exc)
            if self._route_blocks_fallback(target):
                if isinstance(exc, LocalTranslationConfigurationError):
                    return _not_configured_translation_message(target)
                return _unavailable_translation_message(target)
            fallback = await self._fallback(text, source_language, target_language, failed_engine=getattr(engine, "name", None))
            if fallback is not None:
                if engine is not None and hasattr(engine, "record_fallback"):
                    engine.record_fallback()
                return fallback
            raise

    async def _translate_with_timeout(self, engine, text: str, source_language: str, target_language: str) -> str:
        timeout = float(getattr(engine, "timeout_seconds", None) or self.timeout_seconds)
        try:
            return await asyncio.wait_for(engine.translate(text, source_language, target_language), timeout=timeout)
        except asyncio.TimeoutError as exc:
            self.translation_timeouts_count += 1
            if hasattr(engine, "record_timeout"):
                engine.record_timeout(f"{engine.name} timeout after {timeout:g}s for {target_language}")
            raise LocalTranslationTimeoutError(f"{engine.name} timeout after {timeout:g}s for {target_language}") from exc

    async def _fallback(self, text: str, source_language: str, target_language: str, *, failed_engine: str | None) -> str | None:
        fallback_engine = self.engines.get(self.fallback_engine)
        if fallback_engine is not None and fallback_engine.name != failed_engine and fallback_engine.supports(source_language, target_language):
            try:
                self.translation_fallbacks_count += 1
                return await self._translate_with_timeout(fallback_engine, text, source_language, target_language)
            except Exception as exc:
                self._record_error(exc)
        if self.fallback_provider is not None:
            self.translation_fallbacks_count += 1
            translations = await self.fallback_provider.translate_many(text, source_language, [target_language])
            return translations[target_language]
        return None

    def _engine_for_target(self, source_language: str, target_language: str):
        target = normalize_translation_language(target_language)
        if self.routing_enabled:
            routed_engine = self.route_table.get(target)
            if routed_engine == DISABLED_TRANSLATION_ROUTE:
                return None
            if routed_engine:
                return self.engines.get(routed_engine)
            if not self.route_table:
                if target in {"kk", "uz"}:
                    return self.engines.get("tilmash")
                if target == "zh-Hans":
                    return self.engines.get("madlad400")
        default = self.engines.get(self.default_engine)
        if default is not None and default.supports(source_language, target_language):
            return default
        for engine in self.engines.values():
            if engine.supports(source_language, target_language):
                return engine
        return None

    def _route_is_disabled(self, target_language: str) -> bool:
        target = normalize_translation_language(target_language)
        return bool(self.routing_enabled and self.route_table.get(target) == DISABLED_TRANSLATION_ROUTE)

    def _route_blocks_fallback(self, target_language: str) -> bool:
        target = normalize_translation_language(target_language)
        return bool(self.routing_enabled and target == "uz" and self.route_table.get(target) == "m2m100_1_2b_ct2")

    def _route_status_by_language(self, engine_statuses: dict[str, dict]) -> dict[str, dict]:
        languages = sorted({*LOCAL_TRANSLATION_STATUS_LANGUAGES, *self.route_table.keys()})
        return {language: self._route_status_for_language(language, engine_statuses) for language in languages}

    def _route_status_for_language(self, language: str, engine_statuses: dict[str, dict]) -> dict:
        target = normalize_translation_language(language)
        route = self.route_table.get(target)
        if route == DISABLED_TRANSLATION_ROUTE:
            return {"status": "disabled", "ready": False, "route": DISABLED_TRANSLATION_ROUTE, "missing": []}
        engine_name = route or self._inferred_engine_name_for_target(target)
        experimental = _experimental_route_metadata(target, engine_name)
        if not engine_name:
            return {"status": "not_configured", "ready": False, "route": "", "engine": "", "missing": ["LOCAL_TRANSLATION_ROUTE"], **experimental}
        engine_status = engine_statuses.get(engine_name)
        if engine_status is None:
            return {
                "status": "not_configured",
                "ready": False,
                "route": engine_name,
                "engine": engine_name,
                "missing": [f"LOCAL_TRANSLATION_ENGINE_{engine_name.upper()}"],
                **experimental,
            }
        if engine_status.get("status") == "disabled":
            return {
                "status": "not_configured",
                "ready": False,
                "route": engine_name,
                "engine": engine_name,
                "missing": [_translation_engine_enabled_env(engine_name)],
                **experimental,
            }
        status = engine_status.get("status", "not_configured")
        ready = bool(engine_status.get("ready"))
        if experimental:
            if ready or status in {"ready", "configured", "loaded", "degraded"}:
                status = "degraded"
            experimental["warning"] = "Uzbek quality failed automatic benchmark and requires manual review."
        return {
            "status": status,
            "ready": ready,
            "route": engine_name,
            "engine": engine_name,
            "missing": list(engine_status.get("missing", [])),
            **experimental,
        }

    def _inferred_engine_name_for_target(self, target: str) -> str:
        if not self.routing_enabled:
            return self.default_engine
        if not self.route_table:
            if target in {"kk", "uz"}:
                return "tilmash"
            if target == "zh-Hans":
                return "madlad400"
        return self.default_engine

    def _record_error(self, exc: Exception) -> None:
        self.translation_errors_count += 1
        self.translation_last_error = sanitize_error_message(str(exc) or exc.__class__.__name__)


def local_translation_provider_kwargs(settings) -> dict:
    return {
        "enabled": bool(getattr(settings, "local_translation_enabled", False)),
        "routing_enabled": bool(getattr(settings, "local_translation_routing_enabled", True)),
        "default_engine": getattr(settings, "local_translation_default_engine", "tilmash"),
        "fallback_engine": getattr(settings, "local_translation_fallback_engine", "madlad400"),
        "route_table": {
            "kk": getattr(settings, "local_translation_route_kk", ""),
            "uz": getattr(settings, "local_translation_route_uz", ""),
            "zh-Hans": getattr(settings, "local_translation_route_zh", ""),
        },
        "timeout_seconds": getattr(settings, "local_translation_timeout_seconds", 2.5),
        "tilmash_enabled": bool(getattr(settings, "tilmash_enabled", True)),
        "tilmash_model_path": getattr(settings, "tilmash_model_path", ""),
        "tilmash_tokenizer_path": getattr(settings, "tilmash_tokenizer_path", ""),
        "tilmash_server_url": getattr(settings, "tilmash_server_url", ""),
        "tilmash_server_timeout_seconds": getattr(settings, "tilmash_server_timeout_seconds", 1.5),
        "tilmash_device": getattr(settings, "tilmash_device", "cuda"),
        "tilmash_dtype": getattr(settings, "tilmash_dtype", "auto"),
        "tilmash_max_batch_size": getattr(settings, "tilmash_max_batch_size", 8),
        "tilmash_max_new_tokens": getattr(settings, "tilmash_max_new_tokens", 128),
        "tilmash_num_beams": getattr(settings, "tilmash_num_beams", 1),
        "tilmash_timeout_seconds": getattr(settings, "tilmash_timeout_seconds", 1.5),
        "tilmash_load_on_startup": bool(getattr(settings, "tilmash_load_on_startup", False)),
        "madlad_enabled": bool(getattr(settings, "madlad_enabled", False)),
        "madlad_model_path": getattr(settings, "madlad_model_path", ""),
        "madlad_tokenizer_path": getattr(settings, "madlad_tokenizer_path", ""),
        "madlad_server_url": getattr(settings, "madlad_server_url", ""),
        "madlad_server_timeout_seconds": getattr(settings, "madlad_server_timeout_seconds", 4.0),
        "madlad_device": getattr(settings, "madlad_device", "cuda"),
        "madlad_dtype": getattr(settings, "madlad_dtype", "auto"),
        "madlad_quantization": getattr(settings, "madlad_quantization", "8bit"),
        "madlad_max_batch_size": getattr(settings, "madlad_max_batch_size", 4),
        "madlad_timeout_seconds": getattr(settings, "madlad_timeout_seconds", 4.0),
        "madlad_load_on_startup": bool(getattr(settings, "madlad_load_on_startup", False)),
        "m2m100_ct2_enabled": bool(getattr(settings, "m2m100_ct2_enabled", False)),
        "m2m100_ct2_model_path": getattr(settings, "m2m100_ct2_model_path", ""),
        "m2m100_ct2_tokenizer_path": getattr(settings, "m2m100_ct2_tokenizer_path", ""),
        "m2m100_ct2_device": getattr(settings, "m2m100_ct2_device", "cpu"),
        "m2m100_ct2_compute_type": getattr(settings, "m2m100_ct2_compute_type", "int8"),
        "m2m100_ct2_timeout_seconds": getattr(settings, "m2m100_ct2_timeout_seconds", 5.0),
        "m2m100_ct2_load_on_startup": bool(getattr(settings, "m2m100_ct2_load_on_startup", False)),
        "m2m100_ct2_default_size": getattr(settings, "m2m100_ct2_default_size", "418m"),
        "m2m100_ct2_supported_targets": getattr(settings, "m2m100_ct2_supported_targets", "uz,zh-Hans"),
        "m2m100_1_2b_ct2_enabled": bool(getattr(settings, "m2m100_1_2b_ct2_enabled", False)),
        "m2m100_1_2b_ct2_model_path": getattr(settings, "m2m100_1_2b_ct2_model_path", ""),
        "m2m100_1_2b_ct2_tokenizer_path": getattr(settings, "m2m100_1_2b_ct2_tokenizer_path", ""),
        "m2m100_1_2b_ct2_device": getattr(settings, "m2m100_1_2b_ct2_device", "cpu"),
        "m2m100_1_2b_ct2_compute_type": getattr(settings, "m2m100_1_2b_ct2_compute_type", "int8"),
        "m2m100_1_2b_ct2_timeout_seconds": getattr(settings, "m2m100_1_2b_ct2_timeout_seconds", 8.0),
        "m2m100_1_2b_ct2_load_on_startup": bool(getattr(settings, "m2m100_1_2b_ct2_load_on_startup", False)),
        "m2m100_1_2b_ct2_supported_targets": getattr(settings, "m2m100_1_2b_ct2_supported_targets", "uz"),
    }


def _fallback_provider_for_engine(fallback_engine: str) -> TranslationProvider | None:
    if fallback_engine == "mock":
        from app.translation.mock_translator import MockTranslator

        return MockTranslator()
    return None


def _normalize_route_table(route_table: dict[str, str] | None) -> dict[str, str]:
    routes: dict[str, str] = {}
    for raw_target, raw_engine in (route_table or {}).items():
        engine = (raw_engine or "").strip().lower()
        if not engine:
            continue
        target = normalize_translation_language(raw_target)
        routes[target] = engine
    return routes


def _disabled_translation_message(target_language: str) -> str:
    return f"Translation disabled for {normalize_translation_language(target_language)}"


def _not_configured_translation_message(target_language: str) -> str:
    return f"Translation not configured for {normalize_translation_language(target_language)}"


def _unavailable_translation_message(target_language: str) -> str:
    return f"Translation unavailable for {normalize_translation_language(target_language)}"


def _experimental_route_metadata(target_language: str, engine_name: str) -> dict:
    if normalize_translation_language(target_language) == "uz" and engine_name == "m2m100_1_2b_ct2":
        return {"experimental": True, "production_ready": False}
    return {}


def _translation_engine_enabled_env(engine_name: str) -> str:
    return {
        "tilmash": "TILMASH_ENABLED",
        "madlad400": "MADLAD_ENABLED",
        "m2m100_ct2": "M2M100_CT2_ENABLED",
        "m2m100_1_2b_ct2": "M2M100_1_2B_CT2_ENABLED",
    }.get(engine_name, f"LOCAL_TRANSLATION_ENGINE_{engine_name.upper()}_ENABLED")
