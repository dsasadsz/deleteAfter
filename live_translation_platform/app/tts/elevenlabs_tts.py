from datetime import datetime

import httpx

from app.tts.base import TTSConfigurationError, TTSProvider, TTSResult, TTSSynthesisError
from app.tts.voice_catalog import SUPPORTED_TTS_LANGUAGE_ORDER, dedupe_voices, empty_voice_catalog, generic_voice, language_from_locale


_VOICE_CACHE: dict[str, dict[str, list[dict]]] = {}


class ElevenLabsTTS(TTSProvider):
    name = "elevenlabs"

    def __init__(
        self,
        api_key: str,
        voices: dict[str, list[str]] | None = None,
        timeout_seconds: float = 15.0,
        http_client=None,
    ) -> None:
        self.api_key = api_key
        self.voices = voices or {}
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client

    def status(self) -> dict:
        catalog = self.voice_catalog()
        configured = bool(self.api_key)
        ready = configured and any(catalog.values())
        status = "ready" if ready else ("experimental" if configured else "not_configured")
        return {
            "ready": ready,
            "status": status,
            "missing": [] if configured else ["ELEVENLABS_API_KEY"],
            "supported_languages": list(SUPPORTED_TTS_LANGUAGE_ORDER),
            "voices": catalog,
            "default_voice_by_language": {language: voices[0]["id"] if voices else "" for language, voices in catalog.items()},
            "experimental": True,
        }

    def voice_catalog(self) -> dict[str, list[dict]]:
        catalog = empty_voice_catalog()
        for language, voice_ids in self.voices.items():
            for voice_id in voice_ids:
                catalog.setdefault(language, []).append(
                    generic_voice(voice_id, voice_id, "unknown", self.name, language, experimental=True)
                )
        if any(catalog.values()) or not self.api_key:
            return catalog
        return self._discover_voices()

    async def synthesize(
        self,
        text: str,
        language: str,
        voice: str | None = None,
        audio_format: str | None = None,
        metadata: dict | None = None,
        voice_gender: str | None = None,
    ) -> TTSResult:
        selected_voice = voice or self.status()["default_voice_by_language"].get(language)
        if not self.api_key:
            raise TTSConfigurationError("Missing ElevenLabs TTS configuration: ELEVENLABS_API_KEY")
        if not selected_voice:
            raise TTSConfigurationError(f"Missing ElevenLabs TTS voice for {language}")

        started_at = datetime.utcnow()
        client = httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{selected_voice}",
                headers={"xi-api-key": self.api_key, "Accept": "audio/mpeg", "Content-Type": "application/json"},
                json={"text": text, "model_id": "eleven_multilingual_v2"},
            )
            if response.status_code >= 400:
                raise TTSSynthesisError(f"ElevenLabs TTS request failed with status {response.status_code}")
        except TTSSynthesisError:
            raise
        except httpx.HTTPError as exc:
            raise TTSSynthesisError(f"ElevenLabs TTS request failed: {exc}") from exc
        finally:
            await client.aclose()

        latency_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)
        return TTSResult(
            audio_bytes=response.content,
            content_type="audio/mpeg",
            language=language,
            voice=selected_voice,
            provider=self.name,
            duration_ms=None,
            text_chars=len(text),
            cached=False,
            latency_ms=latency_ms,
            metadata={"experimental": True, "voice_gender": voice_gender or "auto", **(metadata or {})},
        )

    def _discover_voices(self) -> dict[str, list[dict]]:
        cache_key = "elevenlabs:user-voices"
        if cache_key in _VOICE_CACHE:
            return _VOICE_CACHE[cache_key]
        catalog = empty_voice_catalog()
        client = self.http_client
        close_client = False
        if client is None:
            client = httpx.Client(timeout=min(self.timeout_seconds, 5.0))
            close_client = True
        try:
            for item in self._voice_items(client):
                labels = item.get("labels") or {}
                language = _language_from_elevenlabs_item(item, labels)
                voice_id = item.get("voice_id") or item.get("voiceId") or item.get("id")
                if language not in catalog or not voice_id:
                    continue
                catalog[language].append(
                    generic_voice(
                        voice_id,
                        item.get("name") or voice_id,
                        labels.get("gender") or item.get("gender") or "unknown",
                        self.name,
                        language,
                        locale=labels.get("locale") or labels.get("language"),
                        experimental=True,
                    )
                )
        except (httpx.HTTPError, ValueError, TypeError):
            return empty_voice_catalog()
        finally:
            if close_client:
                client.close()
        catalog = {language: dedupe_voices(voices) for language, voices in catalog.items()}
        _VOICE_CACHE[cache_key] = catalog
        return catalog

    def _voice_items(self, client) -> list[dict]:
        response = client.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": self.api_key})
        if response.status_code >= 400:
            return []
        payload = response.json()
        voices = payload.get("voices") if isinstance(payload, dict) else payload
        return voices if isinstance(voices, list) else []


def _language_from_elevenlabs_item(item: dict, labels: dict) -> str | None:
    for key in ("language", "locale", "accent"):
        language = language_from_locale(labels.get(key) or item.get(key))
        if language:
            return language
    return None
