from datetime import datetime
import math
import struct
import wave
from io import BytesIO

from app.tts.base import TTSProvider, TTSResult


MOCK_TTS_LANGUAGES = ("kk", "uz", "zh-Hans", "ru")
MOCK_DEFAULT_VOICE_BY_LANGUAGE = {
    "kk": "mock-kk-1",
    "uz": "mock-uz-1",
    "zh-Hans": "mock-zh-1",
    "ru": "mock-ru-1",
}
MOCK_VOICE_PREFIX_BY_LANGUAGE = {"kk": "mock-kk", "uz": "mock-uz", "zh-Hans": "mock-zh", "ru": "mock-ru"}


class MockTTS(TTSProvider):
    name = "mock"

    def status(self) -> dict:
        return {
            "ready": True,
            "status": "ready",
            "missing": [],
            "supported_languages": list(MOCK_TTS_LANGUAGES),
            "voices": {
                language: [
                    _voice(language, 1),
                    _voice(language, 2),
                ]
                for language in MOCK_TTS_LANGUAGES
            },
            "default_voice_by_language": dict(MOCK_DEFAULT_VOICE_BY_LANGUAGE),
            "experimental": False,
        }

    async def synthesize(
        self,
        text: str,
        language: str,
        voice: str | None = None,
        audio_format: str | None = None,
        metadata: dict | None = None,
        voice_gender: str | None = None,
    ) -> TTSResult:
        started_at = datetime.utcnow()
        audio_bytes = _tiny_wav_bytes()
        latency_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)
        selected_voice = voice or MOCK_DEFAULT_VOICE_BY_LANGUAGE.get(language) or f"mock-{language}-1"
        return TTSResult(
            audio_bytes=audio_bytes,
            content_type="audio/wav",
            language=language,
            voice=selected_voice,
            provider=self.name,
            duration_ms=750,
            text_chars=len(text),
            cached=False,
            latency_ms=latency_ms,
            metadata={"mock": True, "voice_gender": voice_gender or "auto", **(metadata or {})},
        )


def _voice(language: str, index: int) -> dict:
    voice_id = f"{MOCK_VOICE_PREFIX_BY_LANGUAGE[language]}-{index}"
    return {
        "id": voice_id,
        "name": f"Mock {language} {index}",
        "display_name": f"Mock {language} voice {index}",
        "gender": "unknown",
        "provider": "mock",
        "language": language,
        "experimental": False,
    }


def _tiny_wav_bytes() -> bytes:
    sample_rate = 16000
    duration_seconds = 0.75
    frequency = 880.0
    amplitude = 0.35
    frame_count = int(sample_rate * duration_seconds)
    buffer = BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        for index in range(frame_count):
            envelope = min(1.0, index / 800, (frame_count - index) / 800)
            sample = int(32767 * amplitude * envelope * math.sin(2 * math.pi * frequency * index / sample_rate))
            writer.writeframesraw(struct.pack("<h", sample))
    return buffer.getvalue()
