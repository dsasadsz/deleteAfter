import asyncio
import json
from datetime import datetime

import pytest

from app.audio.mock_audio_source import MockAudioSource
from app.realtime.audio_pipeline import AudioPipeline
from app.stt.base import create_stt_provider
from app.stt.elevenlabs_stt import (
    ElevenLabsSTTProvider,
    ProviderConfigurationError,
    parse_elevenlabs_message,
)
from app.translation.mock_translator import MockTranslator


@pytest.mark.asyncio
async def test_elevenlabs_provider_refuses_missing_api_key():
    provider = ElevenLabsSTTProvider(api_key="", websocket_client_factory=lambda *_args, **_kwargs: None)

    with pytest.raises(ProviderConfigurationError, match="ELEVENLABS_API_KEY"):
        await provider.connect()


def test_provider_factory_returns_elevenlabs_provider():
    provider = create_stt_provider("elevenlabs", api_key="key")

    assert isinstance(provider, ElevenLabsSTTProvider)
    assert provider.name == "elevenlabs"


def test_elevenlabs_parser_converts_partial_response_to_stt_event():
    event = parse_elevenlabs_message(
        {"message_type": "partial_transcript", "text": "Сегодня мы"},
        audio_received_at=datetime.utcnow(),
        language="ru",
    )

    assert event is not None
    assert event.is_partial is True
    assert event.is_final is False
    assert event.text == "Сегодня мы"
    assert event.provider == "elevenlabs"


def test_elevenlabs_parser_converts_committed_response_to_final_stt_event():
    event = parse_elevenlabs_message(
        {"message_type": "committed_transcript_with_timestamps", "text": "Сегодня мы изучим C#", "language_code": "ru"},
        audio_received_at=datetime.utcnow(),
        language="ru",
    )

    assert event is not None
    assert event.is_partial is False
    assert event.is_final is True
    assert event.language == "ru"
    assert event.raw["message_type"] == "committed_transcript_with_timestamps"


@pytest.mark.asyncio
async def test_elevenlabs_error_response_is_exposed_as_provider_error():
    websocket = FakeWebSocket([{"message_type": "input_error", "message": "Bad audio"}])
    provider = ElevenLabsSTTProvider(api_key="key", websocket_client_factory=FakeWebSocketFactory(websocket))
    await provider.connect()
    await asyncio.sleep(0)

    assert provider.errors_count == 1
    assert provider.last_error == "Bad audio"

    await provider.close()


@pytest.mark.asyncio
async def test_elevenlabs_send_audio_sends_input_audio_chunk_message():
    websocket = FakeWebSocket([])
    provider = ElevenLabsSTTProvider(api_key="key", websocket_client_factory=FakeWebSocketFactory(websocket))
    await provider.connect()
    await provider.send_audio(b"abc", {"sample_rate": 16000, "format": "L16", "audio_received_at": datetime.utcnow()})

    sent = json.loads(websocket.sent_messages[-1])
    assert sent["message_type"] == "input_audio_chunk"
    assert sent["sample_rate"] == 16000
    assert sent["audio_base_64"]

    await provider.close()


@pytest.mark.asyncio
async def test_elevenlabs_send_audio_can_commit_final_chunk():
    websocket = FakeWebSocket([])
    provider = ElevenLabsSTTProvider(api_key="key", commit_strategy="manual", websocket_client_factory=FakeWebSocketFactory(websocket))
    await provider.connect()
    await provider.send_audio(
        b"abc",
        {"sample_rate": 16000, "format": "L16", "audio_received_at": datetime.utcnow(), "finalize": True},
    )

    sent = json.loads(websocket.sent_messages[-1])
    assert sent["message_type"] == "input_audio_chunk"
    assert sent["commit"] is True
    assert sent["audio_base_64"] == ""

    await provider.close()


@pytest.mark.asyncio
async def test_elevenlabs_commit_sends_empty_manual_commit_message():
    websocket = FakeWebSocket([])
    provider = ElevenLabsSTTProvider(api_key="key", commit_strategy="manual", websocket_client_factory=FakeWebSocketFactory(websocket))
    await provider.connect()
    await provider.commit()

    sent = json.loads(websocket.sent_messages[-1])
    assert sent["message_type"] == "input_audio_chunk"
    assert sent["commit"] is True
    assert sent["audio_base_64"] == ""
    assert provider.finalize_sent_at is not None

    await provider.close()


@pytest.mark.asyncio
async def test_audio_pipeline_can_use_mocked_elevenlabs_provider_and_emit_captions():
    events = []

    provider = ScriptedElevenLabsProvider()
    pipeline = AudioPipeline(
        lesson_id="lesson_el",
        meeting_id="123",
        source=MockAudioSource(interval_seconds=0.01, max_chunks=1),
        stt=provider,
        translator=MockTranslator(),
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
    assert final_events[0]["provider"]["stt"] == "elevenlabs"
    assert final_events[0]["original_text"] == "Сегодня мы изучим C#"


async def _append(items, item):
    items.append(item)


class FakeWebSocket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent_messages = []
        self.closed = False

    async def send(self, message):
        self.sent_messages.append(message)

    async def recv(self):
        if not self.incoming:
            await asyncio.sleep(0.05)
            raise asyncio.CancelledError()
        return json.dumps(self.incoming.pop(0))

    async def close(self):
        self.closed = True


class FakeWebSocketFactory:
    def __init__(self, websocket):
        self.websocket = websocket
        self.calls = []

    async def __call__(self, uri, headers, timeout):
        self.calls.append({"uri": uri, "headers": headers, "timeout": timeout})
        return self.websocket


class ScriptedElevenLabsProvider:
    name = "elevenlabs"

    def __init__(self):
        self._queue = asyncio.Queue()

    async def connect(self):
        return None

    async def send_audio(self, audio_chunk, metadata=None):
        from app.stt.base import STTEvent

        await self._queue.put(
            STTEvent(
                text="Сегодня мы изучим C#",
                is_partial=False,
                is_final=True,
                language="ru",
                confidence=None,
                provider="elevenlabs",
                timestamp=datetime.utcnow(),
                raw={"message_type": "committed_transcript"},
                audio_received_at=(metadata or {}).get("audio_received_at"),
            )
        )

    async def events(self):
        while True:
            yield await self._queue.get()

    async def close(self):
        return None
