from datetime import datetime
import asyncio

import pytest

from app.realtime.audio_pipeline import AudioPipeline
from app.stt.base import STTEvent, create_stt_provider
from app.stt.cartesia_stt import (
    CartesiaSTTConfigurationError,
    CartesiaSTTProvider,
    parse_cartesia_error,
    parse_cartesia_message,
)
from app.translation.mock_translator import MockTranslator


@pytest.mark.asyncio
async def test_cartesia_stt_missing_key_gives_clear_configuration_error():
    provider = CartesiaSTTProvider(api_key="")

    with pytest.raises(CartesiaSTTConfigurationError, match="CARTESIA_API_KEY"):
        await provider.connect()


def test_provider_factory_returns_cartesia_stt_provider():
    provider = create_stt_provider("cartesia", api_key="key")

    assert isinstance(provider, CartesiaSTTProvider)
    assert provider.name == "cartesia"


def test_cartesia_parser_converts_partial_response_to_stt_event():
    event = parse_cartesia_message(
        {"type": "transcript", "is_final": False, "text": "частичный текст", "language": "ru"},
        datetime(2026, 5, 8, 10, 0, 0),
        "ru",
    )

    assert event is not None
    assert event.text == "частичный текст"
    assert event.is_partial is True
    assert event.is_final is False
    assert event.provider == "cartesia"


def test_cartesia_parser_converts_interim_alias_to_partial_event():
    event = parse_cartesia_message(
        {"type": "interim", "text": "промежуточный текст"},
        datetime(2026, 5, 8, 10, 0, 0),
        "ru",
    )

    assert event is not None
    assert event.is_partial is True
    assert event.is_final is False


def test_cartesia_parser_converts_final_response_to_stt_event():
    event = parse_cartesia_message(
        {"type": "transcript", "is_final": True, "text": "финальный текст", "language": "ru"},
        datetime(2026, 5, 8, 10, 0, 0),
        "ru",
    )

    assert event is not None
    assert event.text == "финальный текст"
    assert event.is_partial is False
    assert event.is_final is True


def test_cartesia_parser_handles_error_response_cleanly():
    message = {"type": "error", "title": "Invalid model", "message": "bad model", "error_code": "model_not_found"}

    assert parse_cartesia_message(message, datetime.utcnow(), "ru") is None
    error = parse_cartesia_error(message)
    assert error.message == "bad model"
    assert error.error_code == "model_not_found"


@pytest.mark.asyncio
async def test_cartesia_provider_sends_binary_audio_and_yields_events():
    websocket = FakeCartesiaWebSocket(
        [
            {"type": "transcript", "is_final": False, "text": "часть", "language": "ru"},
            {"type": "transcript", "is_final": True, "text": "финал", "language": "ru"},
        ]
    )
    provider = CartesiaSTTProvider(api_key="key", websocket_client_factory=FakeWebSocketFactory(websocket))

    await provider.connect()
    await provider.send_audio(b"\x00\x00", {"sample_rate": 16000, "format": "pcm_s16le"})

    events = []
    async for event in provider.events():
        events.append(event)
        if event.is_final:
            break
    await provider.close()

    assert websocket.sent_messages[0] == b"\x00\x00"
    assert [event.is_partial for event in events] == [True, False]
    assert provider.audio_chunks_sent == 1
    assert provider.audio_bytes_sent == 2


@pytest.mark.asyncio
async def test_audio_pipeline_works_with_fake_cartesia_provider():
    events = []

    async def publish(payload):
        events.append(payload)

    pipeline = AudioPipeline(
        lesson_id="lesson_cartesia",
        meeting_id="123",
        source=FakeAudioSource(),
        stt=FakeCartesiaProvider(),
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
    assert final_events[0]["provider"]["stt"] == "cartesia"
    assert set(final_events[0]["translations"]) == {"kk", "uz"}


class FakeCartesiaWebSocket:
    def __init__(self, messages: list[dict]) -> None:
        import json

        self.messages = [json.dumps(message) for message in messages]
        self.sent_messages = []

    async def send(self, message):
        self.sent_messages.append(message)

    async def recv(self):
        if not self.messages:
            await asyncio.sleep(0.01)
            raise RuntimeError("closed")
        return self.messages.pop(0)

    async def close(self):
        return None


class FakeWebSocketFactory:
    def __init__(self, websocket: FakeCartesiaWebSocket) -> None:
        self.websocket = websocket
        self.calls = []

    async def __call__(self, uri: str, headers: dict[str, str], timeout: float):
        self.calls.append((uri, headers, timeout))
        return self.websocket


class FakeAudioSource:
    name = "fake"

    async def chunks(self):
        from app.audio.base import AudioChunk

        yield AudioChunk(data=b"pcm", source="fake", metadata={"text": "ignored"})

    async def close(self):
        return None


class FakeCartesiaProvider:
    name = "cartesia"

    async def connect(self):
        return None

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None):
        self.metadata = metadata or {}

    async def events(self):
        yield STTEvent(
            text="Cartesia final text",
            is_partial=False,
            is_final=True,
            language="ru",
            confidence=None,
            provider="cartesia",
            timestamp=datetime.utcnow(),
            audio_received_at=datetime.utcnow(),
            raw={"cartesia": True},
        )

    async def close(self):
        return None
