from datetime import datetime
from statistics import mean

import httpx

from app.translation.base import TranslationProvider


class TranslationConfigurationError(RuntimeError):
    pass


class AzureTranslationError(RuntimeError):
    pass


class AzureTranslator(TranslationProvider):
    name = "azure"

    def __init__(
        self,
        api_key: str,
        region: str = "",
        endpoint: str = "https://api.cognitive.microsofttranslator.com",
        api_version: str = "3.0",
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.region = region
        self.endpoint = endpoint.rstrip("/")
        self.api_version = api_version
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self.translation_requests_count = 0
        self.translation_errors_count = 0
        self.translation_last_error: str | None = None
        self.translation_last_success_at: datetime | None = None
        self._latencies_ms: list[int] = []

    @property
    def translation_avg_latency_ms(self) -> float:
        return round(mean(self._latencies_ms), 1) if self._latencies_ms else 0.0

    async def translate_many(
        self,
        text: str,
        source_language: str,
        target_languages: list[str],
    ) -> dict[str, str]:
        if not self.api_key:
            raise TranslationConfigurationError("Missing AZURE_TRANSLATOR_KEY for TRANSLATION_PROVIDER=azure.")
        if not text:
            return {language: "" for language in target_languages}

        started_at = datetime.utcnow()
        close_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(timeout=self.timeout_seconds)
        params: list[tuple[str, str]] = [("api-version", self.api_version), ("from", source_language)]
        params.extend(("to", language) for language in target_languages)
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Content-Type": "application/json; charset=UTF-8",
        }
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region

        self.translation_requests_count += 1
        try:
            response = await client.post(
                f"{self.endpoint}/translate",
                params=params,
                headers=headers,
                json=[{"Text": text}],
            )
        except httpx.HTTPError as exc:
            self._record_error(f"Azure Translator request failed: {exc}")
            raise AzureTranslationError(self.translation_last_error) from exc
        finally:
            if close_client:
                await client.aclose()

        latency = int((datetime.utcnow() - started_at).total_seconds() * 1000)
        if response.status_code >= 400:
            self._record_error(f"Azure Translator failed ({response.status_code}): {self._error_message(response)}")
            raise AzureTranslationError(self.translation_last_error)

        translations = parse_azure_translate_response(response.json(), target_languages)
        self._latencies_ms.append(latency)
        self.translation_last_success_at = datetime.utcnow()
        return translations

    def _record_error(self, message: str) -> None:
        self.translation_errors_count += 1
        self.translation_last_error = message

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(payload)


def parse_azure_translate_response(payload: list, target_languages: list[str]) -> dict[str, str]:
    translations_by_language: dict[str, str] = {}
    if payload:
        for item in payload[0].get("translations", []):
            language = item.get("to")
            if language:
                translations_by_language[language] = item.get("text", "")
    return {
        language: translations_by_language.get(language, f"Translation unavailable for {language}")
        for language in target_languages
    }

