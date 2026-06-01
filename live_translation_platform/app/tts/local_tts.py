from __future__ import annotations

from datetime import datetime

from app.tts.base import SUPPORTED_TTS_LANGUAGES, TTSConfigurationError, TTSProvider, TTSResult, normalize_tts_language
from app.tts.local_engines.base import LocalTTSEngine, local_voice, unique, voice_env_suffix, voice_id_suffix
from app.tts.local_engines.kazakh_tts2 import KazakhTTS2Engine
from app.tts.local_engines.piper import PiperTTSEngine
from app.tts.local_engines.silero import SileroTTSEngine


DISABLED_TTS_ENGINE = "disabled"
LOCAL_TTS_ENGINE_NAMES = {"piper", "silero", "kazakh_tts2", DISABLED_TTS_ENGINE}


class LocalTTSProvider(TTSProvider):
    name = "local"

    def __init__(
        self,
        *,
        enabled: bool = False,
        default_engine: str = "piper",
        ru_engine: str = "silero",
        kk_engine: str = "piper",
        uz_engine: str = "piper",
        zh_engine: str = "piper",
        allowed_languages: str = "",
        timeout_seconds: float = 5.0,
        engines: dict[str, LocalTTSEngine] | None = None,
        piper_enabled: bool = True,
        piper_bin_path: str = "",
        piper_timeout_seconds: float = 5.0,
        piper_default_voice: str = "",
        piper_voices: dict[str, str] | None = None,
        piper_output_format: str = "wav",
        silero_enabled: bool = False,
        silero_model_path: str = "",
        silero_device: str = "cpu",
        silero_timeout_seconds: float = 5.0,
        silero_language: str = "ru",
        silero_speaker: str = "",
        kazakh_tts2_enabled: bool = False,
        kazakh_tts2_model_path: str = "",
        kazakh_tts2_vocoder_path: str = "",
        kazakh_tts2_tokenizer_path: str = "",
        kazakh_tts2_server_url: str = "",
        kazakh_tts2_server_timeout_seconds: float = 5.0,
        kazakh_tts2_device: str = "cuda",
        kazakh_tts2_dtype: str = "auto",
        kazakh_tts2_timeout_seconds: float = 5.0,
        kazakh_tts2_load_on_startup: bool = False,
        kazakh_tts2_output_format: str = "wav",
        kazakh_tts2_default_voice: str = "",
    ) -> None:
        self.enabled = bool(enabled)
        self.default_engine = _normalize_engine_name(default_engine or "piper")
        self.language_engines = {
            "ru": _normalize_engine_name(ru_engine or "silero"),
            "kk": _normalize_engine_name(kk_engine or "piper"),
            "uz": _normalize_engine_name(uz_engine or "piper"),
            "zh-Hans": _normalize_engine_name(zh_engine or "piper"),
        }
        self.allowed_languages = _normalize_allowed_languages(allowed_languages)
        self.timeout_seconds = float(timeout_seconds or 5.0)
        self.kazakh_tts2_enabled = bool(kazakh_tts2_enabled)
        self.kazakh_tts2_model_path = kazakh_tts2_model_path or ""
        self.kazakh_tts2_vocoder_path = kazakh_tts2_vocoder_path or ""
        self.kazakh_tts2_tokenizer_path = kazakh_tts2_tokenizer_path or ""
        self.kazakh_tts2_server_url = kazakh_tts2_server_url or ""
        self.kazakh_tts2_server_timeout_seconds = float(kazakh_tts2_server_timeout_seconds or kazakh_tts2_timeout_seconds or self.timeout_seconds)
        self.kazakh_tts2_device = kazakh_tts2_device or "cuda"
        self.kazakh_tts2_dtype = kazakh_tts2_dtype or "auto"
        self.kazakh_tts2_timeout_seconds = float(kazakh_tts2_timeout_seconds or self.timeout_seconds)
        self.kazakh_tts2_load_on_startup = bool(kazakh_tts2_load_on_startup)
        self.kazakh_tts2_output_format = kazakh_tts2_output_format or "wav"
        self.kazakh_tts2_default_voice = kazakh_tts2_default_voice or ""
        self.engines = engines or {
            "piper": PiperTTSEngine(
                enabled=piper_enabled,
                bin_path=piper_bin_path,
                voices=piper_voices,
                default_voice=piper_default_voice,
                timeout_seconds=piper_timeout_seconds or self.timeout_seconds,
                output_format=piper_output_format,
            ),
            "silero": SileroTTSEngine(
                enabled=silero_enabled,
                model_path=silero_model_path,
                device=silero_device,
                timeout_seconds=silero_timeout_seconds or self.timeout_seconds,
                language=silero_language,
                speaker=silero_speaker,
            ),
            "kazakh_tts2": KazakhTTS2Engine(
                enabled=kazakh_tts2_enabled,
                model_path=kazakh_tts2_model_path,
                vocoder_path=kazakh_tts2_vocoder_path,
                tokenizer_path=kazakh_tts2_tokenizer_path,
                server_url=kazakh_tts2_server_url,
                server_timeout_seconds=kazakh_tts2_server_timeout_seconds,
                device=kazakh_tts2_device,
                dtype=kazakh_tts2_dtype,
                timeout_seconds=kazakh_tts2_timeout_seconds or self.timeout_seconds,
                load_on_startup=kazakh_tts2_load_on_startup,
                output_format=kazakh_tts2_output_format,
                default_voice=kazakh_tts2_default_voice,
            ),
        }
        self.request_count = 0
        self.error_count = 0
        self._last_error: str | None = None

    async def synthesize(
        self,
        text: str,
        language: str,
        voice: str | None = None,
        audio_format: str | None = None,
        metadata: dict | None = None,
        voice_gender: str | None = None,
    ) -> TTSResult:
        self.request_count += 1
        started_at = datetime.utcnow()
        language = normalize_tts_language(language)
        if not self.enabled:
            raise TTSConfigurationError("Local TTS is not configured: LOCAL_TTS_ENABLED")
        if self._language_disabled(language):
            self.error_count += 1
            raise TTSConfigurationError(f"Local TTS is disabled for {language}")
        engine_name, engine = self._engine_for_language(language)
        if engine is None:
            self.error_count += 1
            raise TTSConfigurationError(f"Local TTS engine {engine_name} is not configured for {language}")
        engine_status = engine.status_for_language(language)
        if not engine_status.get("ready"):
            self.error_count += 1
            missing = ", ".join(engine_status.get("missing", [])) or engine_status.get("status", "not_configured")
            raise TTSConfigurationError(f"Local TTS engine {engine_name} is not configured: {missing}")
        selected_voice = voice or engine.default_voice_for_language(language)
        if not selected_voice:
            self.error_count += 1
            raise TTSConfigurationError(f"Local TTS voice is not configured for {language}")
        try:
            result = await engine.synthesize(text, language, selected_voice, audio_format)
        except Exception as exc:
            self.error_count += 1
            self._last_error = str(exc)
            raise
        if isinstance(result, tuple):
            audio_bytes, content_type, duration_ms = result
        else:
            audio_bytes = result.audio_bytes
            content_type = result.content_type
            duration_ms = result.duration_ms
        latency_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)
        return TTSResult(
            audio_bytes=audio_bytes,
            content_type=content_type,
            language=language,
            voice=selected_voice,
            provider=self.name,
            duration_ms=duration_ms,
            text_chars=len(text),
            cached=False,
            latency_ms=latency_ms,
            metadata={
                "local": True,
                "engine": engine_name,
                "requested_audio_format": audio_format,
                "voice_gender": voice_gender or "auto",
                **(metadata or {}),
            },
        )

    def status(self) -> dict:
        voices = {language: [] for language in SUPPORTED_TTS_LANGUAGES}
        defaults: dict[str, str] = {}
        missing: list[str] = []
        selected_engine_by_language: dict[str, str] = {}
        language_status: dict[str, dict] = {}
        for language in sorted(SUPPORTED_TTS_LANGUAGES):
            engine_name, engine = self._engine_for_language(language)
            selected_engine_by_language[language] = engine_name
            if self._language_disabled(language):
                language_status[language] = {
                    "ready": False,
                    "status": "disabled",
                    "engine": DISABLED_TTS_ENGINE,
                    "missing": [],
                }
                continue
            if engine is None:
                missing.append(f"LOCAL_TTS_ENGINE_{engine_name.upper()}")
                language_status[language] = {
                    "ready": False,
                    "status": "not_configured",
                    "engine": engine_name,
                    "missing": [f"LOCAL_TTS_ENGINE_{engine_name.upper()}"],
                }
                continue
            status = engine.status_for_language(language)
            language_missing = list(status.get("missing", []))
            language_status_value = status.get("status", "not_configured")
            if language_status_value == "disabled":
                language_status_value = "not_configured"
                language_missing = [_engine_enabled_env(engine_name)]
            language_status[language] = {
                "ready": bool(status.get("ready")),
                "status": language_status_value,
                "engine": engine_name,
                "missing": language_missing,
            }
            if status.get("ready"):
                catalog = engine.voice_catalog()
                voices[language].extend(catalog.get(language, []))
                default_voice = engine.default_voice_for_language(language)
                if default_voice:
                    defaults[language] = default_voice
            else:
                missing.extend(language_missing)
        if not self.enabled:
            missing.insert(0, "LOCAL_TTS_ENABLED")
        ready = self.enabled and bool(defaults)
        return {
            "ready": ready,
            "status": "ready" if ready else "not_configured",
            "missing": unique(missing),
            "supported_languages": sorted(SUPPORTED_TTS_LANGUAGES),
            "allowed_languages": sorted(self.allowed_languages) if self.allowed_languages else sorted(SUPPORTED_TTS_LANGUAGES),
            "voices": voices,
            "default_voice_by_language": defaults,
            "experimental": True,
            "engines": self._engine_statuses(),
            "selected_engine_by_language": selected_engine_by_language,
            "language_status": language_status,
            "default_engine": self.default_engine,
            "language_engines": dict(self.language_engines),
            "metrics": {
                "request_count": self.request_count,
                "error_count": self.error_count,
                "last_error": self._last_error,
            },
        }

    def _engine_for_language(self, language: str) -> tuple[str, LocalTTSEngine | None]:
        language = normalize_tts_language(language)
        if self._language_disabled(language):
            return DISABLED_TTS_ENGINE, None
        preferred = self.language_engines.get(language, self.default_engine)
        if preferred == DISABLED_TTS_ENGINE:
            return DISABLED_TTS_ENGINE, None
        for engine_name in _candidate_engines(preferred, language, self.default_engine):
            if engine_name == DISABLED_TTS_ENGINE:
                continue
            engine = self.engines.get(engine_name)
            if engine is not None and engine.supports(language):
                return engine_name, engine
        engine = self.engines.get(preferred)
        if engine is not None:
            return preferred, engine
        engine = self.engines.get(self.default_engine)
        return self.default_engine, engine

    def _language_disabled(self, language: str) -> bool:
        normalized = normalize_tts_language(language)
        if normalized not in SUPPORTED_TTS_LANGUAGES:
            return True
        if self.allowed_languages and normalized not in self.allowed_languages:
            return True
        return self.language_engines.get(normalized) == DISABLED_TTS_ENGINE

    def _engine_statuses(self) -> dict:
        return {name: engine.status() for name, engine in self.engines.items()}

    async def initialize(self) -> None:
        for engine in self.engines.values():
            if not bool(getattr(engine, "load_on_startup", False)):
                continue
            if hasattr(engine, "initialize"):
                await engine.initialize()


def local_tts_provider_kwargs(settings) -> dict:
    return {
        "enabled": bool(getattr(settings, "local_tts_enabled", False)),
        "default_engine": getattr(settings, "local_tts_default_engine", "piper"),
        "ru_engine": getattr(settings, "local_tts_ru_engine", "silero"),
        "kk_engine": getattr(settings, "local_tts_kk_engine", "piper"),
        "uz_engine": getattr(settings, "local_tts_uz_engine", "piper"),
        "zh_engine": getattr(settings, "local_tts_zh_engine", "piper"),
        "allowed_languages": getattr(settings, "local_tts_allowed_languages", ""),
        "timeout_seconds": getattr(settings, "local_tts_timeout_seconds", 5.0),
        "piper_enabled": bool(getattr(settings, "piper_enabled", True)),
        "piper_bin_path": getattr(settings, "piper_bin_path", ""),
        "piper_timeout_seconds": getattr(settings, "piper_timeout_seconds", 5.0),
        "piper_default_voice": getattr(settings, "piper_default_voice", ""),
        "piper_voices": {
            "ru": getattr(settings, "piper_voice_ru", ""),
            "kk": getattr(settings, "piper_voice_kk", ""),
            "uz": getattr(settings, "piper_voice_uz", ""),
            "zh-Hans": getattr(settings, "piper_voice_zh", ""),
        },
        "piper_output_format": getattr(settings, "piper_output_format", "wav"),
        "silero_enabled": bool(getattr(settings, "silero_tts_enabled", False)),
        "silero_model_path": getattr(settings, "silero_tts_model_path", ""),
        "silero_device": getattr(settings, "silero_tts_device", "cpu"),
        "silero_timeout_seconds": getattr(settings, "silero_tts_timeout_seconds", 5.0),
        "silero_language": getattr(settings, "silero_tts_language", "ru"),
        "silero_speaker": getattr(settings, "silero_tts_speaker", ""),
        "kazakh_tts2_enabled": bool(getattr(settings, "kazakh_tts2_enabled", False)),
        "kazakh_tts2_model_path": getattr(settings, "kazakh_tts2_model_path", ""),
        "kazakh_tts2_vocoder_path": getattr(settings, "kazakh_tts2_vocoder_path", ""),
        "kazakh_tts2_tokenizer_path": getattr(settings, "kazakh_tts2_tokenizer_path", ""),
        "kazakh_tts2_server_url": getattr(settings, "kazakh_tts2_server_url", ""),
        "kazakh_tts2_server_timeout_seconds": getattr(settings, "kazakh_tts2_server_timeout_seconds", 5.0),
        "kazakh_tts2_device": getattr(settings, "kazakh_tts2_device", "cuda"),
        "kazakh_tts2_dtype": getattr(settings, "kazakh_tts2_dtype", "auto"),
        "kazakh_tts2_timeout_seconds": getattr(settings, "kazakh_tts2_timeout_seconds", 5.0),
        "kazakh_tts2_load_on_startup": bool(getattr(settings, "kazakh_tts2_load_on_startup", False)),
        "kazakh_tts2_output_format": getattr(settings, "kazakh_tts2_output_format", "wav"),
        "kazakh_tts2_default_voice": getattr(settings, "kazakh_tts2_default_voice", ""),
    }


def _candidate_engines(preferred: str, language: str, default_engine: str) -> list[str]:
    candidates = [preferred]
    if language == "ru":
        candidates.extend(["silero", "piper"])
    elif language in {"kk", "uz", "zh-Hans"}:
        candidates.append("piper")
    candidates.append(default_engine)
    return [_normalize_engine_name(item) for item in unique(candidates)]


def _normalize_engine_name(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_")
    return normalized if normalized in LOCAL_TTS_ENGINE_NAMES else value.strip().lower()


def _normalize_allowed_languages(value: str | set[str] | list[str] | tuple[str, ...] | None) -> set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        raw_items = value.split(",")
    else:
        raw_items = value
    return {normalize_tts_language(item) for item in raw_items if normalize_tts_language(item) in SUPPORTED_TTS_LANGUAGES}


def _voice_env_suffix(language: str) -> str:
    return voice_env_suffix(language)


def _voice_id_suffix(language: str) -> str:
    return voice_id_suffix(language)


def _voice(language: str, engine: str, voice_id: str) -> dict:
    return local_voice(language, engine, voice_id)


def _unique(items: list[str]) -> list[str]:
    return unique(items)


def _engine_enabled_env(engine_name: str) -> str:
    return {
        "piper": "PIPER_ENABLED",
        "silero": "SILERO_TTS_ENABLED",
        "kazakh_tts2": "KAZAKH_TTS2_ENABLED",
    }.get(engine_name, f"LOCAL_TTS_ENGINE_{engine_name.upper()}_ENABLED")
