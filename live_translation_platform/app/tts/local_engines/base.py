from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LocalTTSSynthesisResult:
    audio_bytes: bytes
    content_type: str
    duration_ms: int | None = None


class LocalTTSEngine:
    name: str

    def supports(self, language: str) -> bool:
        raise NotImplementedError

    def default_voice_for_language(self, language: str) -> str:
        raise NotImplementedError

    def voice_catalog(self) -> dict[str, list[dict]]:
        raise NotImplementedError

    def status(self) -> dict:
        raise NotImplementedError

    def status_for_language(self, language: str) -> dict:
        return self.status()

    async def synthesize(self, text: str, language: str, voice: str | None = None, audio_format: str | None = None) -> LocalTTSSynthesisResult:
        raise NotImplementedError


def audio_content_type(output_format: str | None) -> str:
    normalized = (output_format or "wav").strip().lower()
    if normalized in {"mp3", "mpeg"}:
        return "audio/mpeg"
    if normalized in {"ogg", "opus"}:
        return "audio/ogg"
    return "audio/wav"


def voice_env_suffix(language: str) -> str:
    return {"zh-Hans": "ZH"}.get(language, language.upper())


def voice_id_suffix(language: str) -> str:
    return {"zh-Hans": "zh"}.get(language, language)


def locale_for_language(language: str) -> str | None:
    return {
        "kk": "kk-KZ",
        "uz": "uz-UZ",
        "zh-Hans": "zh-CN",
        "ru": "ru-RU",
    }.get(language)


def local_voice(language: str, engine: str, voice_id: str) -> dict:
    return {
        "id": voice_id,
        "name": f"{engine} {language}",
        "short_name": voice_id,
        "display_name": f"{engine} {language}",
        "gender": "unknown",
        "provider": "local",
        "language": language,
        "locale": locale_for_language(language),
        "experimental": True,
    }


def sanitize_tts_error(message: object, *redactions: str) -> str:
    text = str(message or "").strip() or "unknown error"
    for value in redactions:
        if value:
            text = text.replace(value, "<redacted>")
            text = text.replace(value.replace("\\", "/"), "<redacted>")
    text = re.sub(r"[A-Za-z]:[\\/][^\s,\"')]+", "<redacted-path>", text)
    text = re.sub(r"/(?:[^/\s,\"')]+/)+[^/\s,\"')]+", "<redacted-path>", text)
    text = re.sub(r"(?i)(token|secret|key|password)[=:\s]+[^\s,\"')]+", r"\1=<redacted>", text)
    return text[:500]


def unique(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
