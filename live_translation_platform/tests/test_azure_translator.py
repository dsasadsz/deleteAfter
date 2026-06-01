import asyncio
import json
from datetime import datetime

import httpx
import pytest

from app.audio.mock_audio_source import MockAudioSource
from app.realtime.audio_pipeline import AudioPipeline
from app.stt.base import STTEvent
from app.translation.azure_translator import (
    AzureTranslationError,
    AzureTranslator,
    TranslationConfigurationError,
)
from app.translation.base import create_translation_provider


@pytest.mark.asyncio
async def test_azure_translator_refuses_missing_key():
    translator = AzureTranslator(api_key="")

    with pytest.raises(TranslationConfigurationError, match="AZURE_TRANSLATOR_KEY"):
        await translator.translate_many("Привет", "ru", ["kk"])


@pytest.mark.asyncio
async def test_azure_translator_builds_correct_multi_target_request():
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/translate"
        params = dict(request.url.params.multi_items())
        assert params["api-version"] == "3.0"
        assert params["from"] == "ru"
        assert request.url.params.get_list("to") == ["kk", "uz", "zh-Hans"]
        assert request.headers["Ocp-Apim-Subscription-Key"] == "key"
        assert request.headers["Ocp-Apim-Subscription-Region"] == "eastus"
        assert json.loads(request.content) == [{"Text": "Сегодня урок"}]
        return httpx.Response(
            200,
            json=[
                {
                    "translations": [
                        {"to": "kk", "text": "Бүгін сабақ"},
                        {"to": "uz", "text": "Bugun dars"},
                        {"to": "zh-Hans", "text": "今天上课"},
                    ]
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.cognitive.microsofttranslator.com") as client:
        translator = AzureTranslator(api_key="key", region="eastus", http_client=client)
        result = await translator.translate_many("Сегодня урок", "ru", ["kk", "uz", "zh-Hans"])

    assert result == {"kk": "Бүгін сабақ", "uz": "Bugun dars", "zh-Hans": "今天上课"}
    assert len(requests) == 1
    assert translator.translation_requests_count == 1
    assert translator.translation_errors_count == 0


@pytest.mark.asyncio
async def test_azure_translator_handles_error_response():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 401000, "message": "Unauthorized"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.cognitive.microsofttranslator.com") as client:
        translator = AzureTranslator(api_key="bad", http_client=client)
        with pytest.raises(AzureTranslationError, match="Unauthorized"):
            await translator.translate_many("Сегодня урок", "ru", ["kk"])

    assert translator.translation_errors_count == 1
    assert "Unauthorized" in translator.translation_last_error


def test_translation_provider_factory_returns_azure_translator():
    provider = create_translation_provider("azure", api_key="key")

    assert isinstance(provider, AzureTranslator)
    assert provider.name == "azure"


@pytest.mark.asyncio
async def test_audio_pipeline_with_azure_translator_emits_all_target_translations():
    events = []
    translator = FakeAzureTranslator()
    pipeline = AudioPipeline(
        lesson_id="lesson_az",
        meeting_id="123",
        source=MockAudioSource(interval_seconds=0.01, max_chunks=1),
        stt=FakeSTT(),
        translator=translator,
        target_languages=["kk", "uz", "zh-Hans"],
        translate_partials=False,
        publish=lambda payload: _append(events, payload),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: None,
    )

    await pipeline.start()
    await asyncio.sleep(0.1)
    await pipeline.stop()

    final_events = [event for event in events if event["is_final"]]
    assert final_events
    assert final_events[0]["provider"]["translator"] == "azure"
    assert set(final_events[0]["translations"]) == {"kk", "uz", "zh-Hans"}
    assert final_events[0]["translations"]["kk"] == "AZ kk: Сегодня урок"


async def _append(items, item):
    items.append(item)


class FakeAzureTranslator:
    name = "azure"
    translation_requests_count = 0
    translation_errors_count = 0
    translation_last_error = None
    translation_last_success_at = None
    translation_avg_latency_ms = 0.0

    async def translate_many(self, text, source_language, target_languages):
        self.translation_requests_count += 1
        self.translation_last_success_at = datetime.utcnow()
        return {language: f"AZ {language}: {text}" for language in target_languages}


class FakeSTT:
    name = "mock"

    async def connect(self):
        return None

    async def send_audio(self, audio_chunk, metadata=None):
        return None

    async def events(self):
        yield STTEvent(
            text="Сегодня урок",
            is_partial=False,
            is_final=True,
            language="ru",
            confidence=None,
            provider="mock",
            timestamp=datetime.utcnow(),
            raw={"audio_source": "mock"},
            audio_received_at=datetime.utcnow(),
        )

    async def close(self):
        return None

