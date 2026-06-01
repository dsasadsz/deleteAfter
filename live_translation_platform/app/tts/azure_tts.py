from datetime import datetime
from html import escape
import os

import httpx

from app.tts.base import TTSConfigurationError, TTSProvider, TTSResult, TTSSynthesisError
from app.tts.voice_catalog import LANGUAGE_TO_LOCALE, azure_voice, dedupe_voices, empty_voice_catalog, language_from_locale


LANGUAGE_TO_VOICE_ENV = {
    "kk": "AZURE_TTS_DEFAULT_VOICE_KK",
    "uz": "AZURE_TTS_DEFAULT_VOICE_UZ",
    "zh-Hans": "AZURE_TTS_DEFAULT_VOICE_ZH",
    "ru": "AZURE_TTS_DEFAULT_VOICE_RU",
}
LANGUAGE_TO_GENDER_VOICE_ENV = {
    "kk": {"male": "AZURE_TTS_VOICE_KK_MALE", "female": "AZURE_TTS_VOICE_KK_FEMALE"},
    "uz": {"male": "AZURE_TTS_VOICE_UZ_MALE", "female": "AZURE_TTS_VOICE_UZ_FEMALE"},
    "zh-Hans": {"male": "AZURE_TTS_VOICE_ZH_MALE", "female": "AZURE_TTS_VOICE_ZH_FEMALE"},
    "ru": {"male": "AZURE_TTS_VOICE_RU_MALE", "female": "AZURE_TTS_VOICE_RU_FEMALE"},
}
DEFAULT_AUDIO_FORMAT = "audio-16khz-32kbitrate-mono-mp3"
_VOICE_LIST_CACHE: dict[str, dict[str, list[dict]]] = {}


class AzureTTS(TTSProvider):
    name = "azure"

    def __init__(
        self,
        api_key: str,
        region: str,
        endpoint: str = "",
        voices: dict[str, str] | None = None,
        voice_lists: dict[str, list[str]] | None = None,
        gender_voices: dict[str, dict[str, str]] | None = None,
        timeout_seconds: float = 15.0,
        http_client=None,
        voice_list_client=None,
    ) -> None:
        self.api_key = api_key
        self.region = region
        self.endpoint = endpoint
        self.voices = voices or {}
        self.voice_lists = voice_lists or {}
        self.gender_voices = gender_voices or {}
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client
        self.voice_list_client = voice_list_client

    def status(self) -> dict:
        voices = self.voice_catalog()
        missing = self._missing_configuration()
        return {
            "ready": not missing,
            "status": "ready" if not missing else "missing_config",
            "missing": missing,
            "supported_languages": list(LANGUAGE_TO_LOCALE),
            "voices": voices,
            "default_voice_by_language": {language: self._default_voice(language, voices) for language in LANGUAGE_TO_LOCALE},
            "experimental": False,
        }

    def voice_catalog(self) -> dict[str, list[dict]]:
        configured = {language: self._configured_voice_catalog(language) for language in LANGUAGE_TO_LOCALE}
        discovered = self._discovered_voice_catalog()
        if not any(discovered.values()):
            return configured
        return discovered

    async def synthesize(
        self,
        text: str,
        language: str,
        voice: str | None = None,
        audio_format: str | None = None,
        metadata: dict | None = None,
        voice_gender: str | None = None,
    ) -> TTSResult:
        selected_voice = self._ensure_ready(language, voice, voice_gender)
        selected_format = audio_format or DEFAULT_AUDIO_FORMAT
        started_at = datetime.utcnow()
        client = self.http_client
        should_close_client = client is None

        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout_seconds)

        try:
            response = await client.post(
                self._url(),
                content=self._ssml(text, language, selected_voice).encode("utf-8"),
                headers={
                    "Ocp-Apim-Subscription-Key": self.api_key,
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": selected_format,
                    "User-Agent": "live_translation_platform",
                },
            )
            if response.status_code >= 400:
                raise TTSSynthesisError(f"Azure TTS request failed with status {response.status_code}")
        except TTSSynthesisError:
            raise
        except httpx.HTTPError as exc:
            raise TTSSynthesisError(f"Azure TTS request failed: {exc}") from exc
        finally:
            if should_close_client:
                await client.aclose()

        latency_ms = int((datetime.utcnow() - started_at).total_seconds() * 1000)
        return TTSResult(
            audio_bytes=response.content,
            content_type=self._content_type(selected_format),
            language=language,
            voice=selected_voice,
            provider=self.name,
            duration_ms=None,
            text_chars=len(text),
            cached=False,
            latency_ms=latency_ms,
            metadata={
                "audio_format": selected_format,
                "voice_gender": voice_gender or "auto",
                **(metadata or {}),
                "azure_request_id": response.headers.get("X-RequestId"),
            },
        )

    def resolve_voice(
        self,
        language: str,
        explicit_voice: str | None = None,
        voice_gender: str | None = None,
    ) -> str:
        if language not in LANGUAGE_TO_LOCALE:
            raise TTSConfigurationError(f"Unsupported Azure TTS language: {language}")
        if explicit_voice:
            return explicit_voice
        normalized_gender = (voice_gender or "auto").strip().lower()
        if normalized_gender in {"male", "female"}:
            gender_voice = self.gender_voices.get(language, {}).get(normalized_gender, "")
            if gender_voice:
                return gender_voice
        selected_voice = self.voices.get(language, "")
        if selected_voice:
            return selected_voice
        configured = self._configured_voice_catalog(language)
        if configured:
            return configured[0]["id"]
        raise TTSConfigurationError(f"Missing Azure TTS configuration: {LANGUAGE_TO_VOICE_ENV[language]}")

    def _missing_configuration(self) -> list[str]:
        missing = []
        if not self.api_key:
            missing.append("AZURE_TTS_KEY")
        if not self.region and not self.endpoint:
            missing.append("AZURE_TTS_REGION")
        catalog = self.voice_catalog()
        for language, env_name in LANGUAGE_TO_VOICE_ENV.items():
            if not catalog.get(language):
                missing.append(env_name)
        return missing

    def _ensure_ready(self, language: str, voice: str | None, voice_gender: str | None = None) -> str:
        if language not in LANGUAGE_TO_LOCALE:
            raise TTSConfigurationError(f"Unsupported Azure TTS language: {language}")
        missing = []
        if not self.api_key:
            missing.append("AZURE_TTS_KEY")
        if not self.region and not self.endpoint:
            missing.append("AZURE_TTS_REGION")
        try:
            selected_voice = self.resolve_voice(language, explicit_voice=voice, voice_gender=voice_gender)
        except TTSConfigurationError:
            missing.append(LANGUAGE_TO_VOICE_ENV[language])
        if missing:
            raise TTSConfigurationError(f"Missing Azure TTS configuration: {', '.join(missing)}")
        return selected_voice

    def _configured_voice_catalog(self, language: str) -> list[dict]:
        voices = []
        seen = set()
        for voice_id in self.voice_lists.get(language, []):
            voices.append(azure_voice(voice_id, language))
            seen.add(voice_id)
        default_voice = self.voices.get(language, "")
        if default_voice and default_voice not in seen:
            voices.append(azure_voice(default_voice, language))
            seen.add(default_voice)
        for gender in ("male", "female"):
            voice_id = self.gender_voices.get(language, {}).get(gender, "")
            if voice_id and voice_id not in seen:
                voices.append(azure_voice(voice_id, language, gender=gender))
                seen.add(voice_id)
        return voices

    def _discovered_voice_catalog(self) -> dict[str, list[dict]]:
        if not self.api_key or (not self.region and not self.endpoint):
            return empty_voice_catalog()
        if os.getenv("IGNORE_DOTENV_IN_TESTS", "").strip().lower() in {"1", "true", "yes", "on"} and self.voice_list_client is None:
            return empty_voice_catalog()
        cache_key = self.endpoint or self.region
        if cache_key in _VOICE_LIST_CACHE:
            return _VOICE_LIST_CACHE[cache_key]
        catalog = empty_voice_catalog()
        client = self.voice_list_client
        close_client = False
        if client is None:
            client = httpx.Client(timeout=min(self.timeout_seconds, 3.0))
            close_client = True
        try:
            response = client.get(
                self._voices_list_url(),
                headers={"Ocp-Apim-Subscription-Key": self.api_key, "User-Agent": "live_translation_platform"},
            )
            if response.status_code >= 400:
                return catalog
            for item in response.json():
                locale = item.get("Locale") or item.get("locale")
                language = _language_for_azure_locale(locale)
                short_name = item.get("ShortName") or item.get("shortName") or item.get("Name") or item.get("name")
                if language and short_name:
                    catalog[language].append(
                        azure_voice(
                            short_name,
                            language,
                            gender=item.get("Gender") or item.get("gender"),
                            name=item.get("DisplayName") or item.get("LocalName") or None,
                            display_name=item.get("DisplayName") or None,
                            local_name=item.get("LocalName") or None,
                            locale=locale,
                        )
                    )
        except (httpx.HTTPError, ValueError, TypeError):
            return empty_voice_catalog()
        finally:
            if close_client:
                client.close()
        catalog = {language: dedupe_voices(voices) for language, voices in catalog.items()}
        _VOICE_LIST_CACHE[cache_key] = catalog
        return catalog

    def _default_voice(self, language: str, catalog: dict[str, list[dict]]) -> str:
        configured = self.voices.get(language, "")
        if configured:
            return configured
        voices = catalog.get(language, [])
        return voices[0]["id"] if voices else ""

    def _url(self) -> str:
        if self.endpoint:
            return f"{self.endpoint.rstrip('/')}/cognitiveservices/v1"
        return f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"

    def _voices_list_url(self) -> str:
        if self.endpoint:
            return f"{self.endpoint.rstrip('/')}/cognitiveservices/voices/list"
        return f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/voices/list"

    def _ssml(self, text: str, language: str, voice: str) -> str:
        locale = LANGUAGE_TO_LOCALE[language]
        return (
            f'<speak version="1.0" xml:lang="{locale}" '
            f'xmlns="http://www.w3.org/2001/10/synthesis">'
            f'<voice name="{escape(voice, quote=True)}">{escape(text)}</voice>'
            "</speak>"
        )

    def _content_type(self, audio_format: str) -> str:
        normalized_format = audio_format.lower()
        if "wav" in normalized_format or normalized_format.startswith("riff-"):
            return "audio/wav"
        return "audio/mpeg"

def _language_for_azure_locale(locale: str | None) -> str | None:
    return language_from_locale(locale)
