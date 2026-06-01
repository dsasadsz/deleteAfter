from datetime import datetime
import asyncio

import pytest

from app.realtime.audio_pipeline import AudioPipeline
from app.stt.azure_stt import (
    AzureSpeechConfigurationError,
    AzureSTTProvider,
    parse_azure_canceled_event,
    parse_azure_recognized_event,
    parse_azure_recognizing_event,
)
from app.stt.base import STTEvent, create_stt_provider
from app.translation.mock_translator import MockTranslator


@pytest.mark.asyncio
async def test_azure_stt_missing_key_gives_clear_configuration_error():
    provider = AzureSTTProvider(api_key="", region="eastus")

    with pytest.raises(AzureSpeechConfigurationError, match="AZURE_SPEECH_KEY"):
        await provider.connect()


@pytest.mark.asyncio
async def test_azure_stt_missing_region_gives_clear_configuration_error():
    provider = AzureSTTProvider(api_key="key", region="")

    with pytest.raises(AzureSpeechConfigurationError, match="AZURE_SPEECH_REGION"):
        await provider.connect()


def test_provider_factory_returns_azure_stt_provider():
    provider = create_stt_provider("azure", api_key="key", region="eastus")

    assert isinstance(provider, AzureSTTProvider)
    assert provider.name == "azure"


def test_azure_recognizing_callback_converts_to_partial_stt_event():
    event = parse_azure_recognizing_event(FakeRecognitionEvent("частичный текст"), datetime(2026, 5, 8, 10, 0, 0), "ru-RU")

    assert event is not None
    assert event.text == "частичный текст"
    assert event.is_partial is True
    assert event.is_final is False
    assert event.language == "ru-RU"
    assert event.provider == "azure"
    assert event.audio_received_at == datetime(2026, 5, 8, 10, 0, 0)


def test_azure_recognized_callback_converts_to_final_stt_event():
    event = parse_azure_recognized_event(FakeRecognitionEvent("финальный текст"), datetime(2026, 5, 8, 10, 0, 0), "ru-RU")

    assert event is not None
    assert event.text == "финальный текст"
    assert event.is_partial is False
    assert event.is_final is True
    assert event.raw["reason"] == "RecognizedSpeech"


def test_azure_no_match_is_ignored_without_caption_event():
    event = parse_azure_recognized_event(FakeRecognitionEvent("", reason="NoMatch"), datetime(2026, 5, 8, 10, 0, 0), "ru-RU")

    assert event is None


def test_azure_canceled_event_extracts_error_message():
    error = parse_azure_canceled_event(FakeCanceledEvent("AuthenticationFailure", "bad key"))

    assert error.reason == "AuthenticationFailure"
    assert error.message == "bad key"


@pytest.mark.asyncio
async def test_audio_pipeline_works_with_fake_azure_provider():
    events = []

    async def publish(payload):
        events.append(payload)

    pipeline = AudioPipeline(
        lesson_id="lesson_azure",
        meeting_id="123",
        source=FakeAudioSource(),
        stt=FakeAzureProvider(),
        translator=MockTranslator(),
        target_languages=["kk", "uz"],
        translate_partials=False,
        publish=publish,
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: None,
    )

    await pipeline.start()
    await asyncio.sleep(0.05)
    await pipeline.stop()

    final_events = [event for event in events if event["is_final"]]
    assert final_events
    assert final_events[0]["provider"]["stt"] == "azure"
    assert set(final_events[0]["translations"]) == {"kk", "uz"}


class FakeRecognitionEvent:
    def __init__(self, text: str, reason: str = "RecognizedSpeech") -> None:
        self.result = type(
            "Result",
            (),
            {
                "text": text,
                "reason": reason,
                "duration": 1000,
                "offset": 2000,
                "json": '{"NBest":[]}',
            },
        )()
        self.session_id = "session_test"


class FakeCanceledEvent:
    def __init__(self, reason: str, details: str) -> None:
        self.reason = reason
        self.error_details = details
        self.session_id = "session_test"


class FakeAudioSource:
    name = "fake"

    async def chunks(self):
        from app.audio.base import AudioChunk

        yield AudioChunk(data=b"pcm", source="fake", metadata={"text": "ignored"})

    async def close(self):
        return None


class FakeAzureProvider:
    name = "azure"

    async def connect(self):
        return None

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None):
        self.metadata = metadata or {}

    async def events(self):
        yield STTEvent(
            text="Azure final text",
            is_partial=False,
            is_final=True,
            language="ru-RU",
            confidence=None,
            provider="azure",
            timestamp=datetime.utcnow(),
            audio_received_at=datetime.utcnow(),
            raw={"azure": True},
        )

    async def close(self):
        return None
