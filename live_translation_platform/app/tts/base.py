from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping


SUPPORTED_TTS_LANGUAGES = {"kk", "uz", "zh-Hans", "ru"}


class TTSConfigurationError(RuntimeError):
    pass


class TTSSynthesisError(RuntimeError):
    pass


@dataclass(frozen=True)
class TTSResult:
    audio_bytes: bytes
    content_type: str
    language: str
    voice: str | None
    provider: str
    duration_ms: int | None
    text_chars: int
    cached: bool
    latency_ms: int
    metadata: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def with_cached(self, cached: bool) -> "TTSResult":
        return replace(self, cached=cached, metadata=dict(self.metadata))


class TTSProvider(ABC):
    name: str

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        language: str,
        voice: str | None = None,
        audio_format: str | None = None,
        metadata: dict | None = None,
        voice_gender: str | None = None,
    ) -> TTSResult:
        pass


def normalize_tts_language(language: str | None) -> str:
    value = (language or "").strip()
    lowered = value.lower().replace("_", "-")
    if lowered in {"kk", "kk-kz"}:
        return "kk"
    if lowered in {"uz", "uz-uz"}:
        return "uz"
    if lowered in {"ru", "ru-ru"}:
        return "ru"
    if lowered in {"zh", "zh-cn", "zh-hans", "zh-hans-cn"}:
        return "zh-Hans"
    return value
