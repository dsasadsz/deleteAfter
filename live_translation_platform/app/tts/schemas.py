from pydantic import BaseModel
from typing import Literal


class TTSVoice(BaseModel):
    id: str
    name: str
    short_name: str | None = None
    display_name: str | None = None
    local_name: str | None = None
    gender: str = "unknown"
    provider: str
    language: str
    locale: str | None = None
    experimental: bool = False


class TTSProviderStatus(BaseModel):
    ready: bool
    status: str
    supported_languages: list[str]
    voices: dict[str, list[TTSVoice]]
    default_voice_by_language: dict[str, str]
    missing: list[str] = []
    experimental: bool = False
    engines: dict = {}
    selected_engine_by_language: dict[str, str] = {}
    language_status: dict = {}
    allowed_languages: list[str] = []


class TTSStatusResponse(BaseModel):
    enabled: bool
    provider: str
    active_provider: str
    ready: bool
    missing: list[str]
    supported_languages: list[str]
    voices: dict[str, list[TTSVoice]]
    default_voice_by_language: dict[str, str]
    providers: dict[str, TTSProviderStatus]
    selected_voice_support: dict
    shared_cache_enabled: bool = False
    audio_url_enabled: bool = False


class TTSSynthesizeRequest(BaseModel):
    text: str
    language: str
    provider: str | None = None
    voice: str | None = None
    voice_gender: str = "auto"
    caption_id: str | None = None
    sequence: int | None = None
    return_mode: Literal["audio", "url"] = "audio"


class TTSAudioURLResponse(BaseModel):
    audio_url: str
    cached: bool
    provider: str
    voice: str | None = None
    language: str
    caption_id: str | None = None
    expires_at: str
    audio_mime_type: str
