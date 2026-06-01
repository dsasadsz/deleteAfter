import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

import pytest

from app.audio.mock_audio_source import MockAudioSource
from app.realtime.audio_pipeline import AudioPipeline
from app.stt.base import create_stt_provider
from app.stt.faster_whisper_stt import FasterWhisperSTTProvider
from app.translation.mock_translator import MockTranslator


class FakeFasterWhisperBackend:
    def __init__(self, text: str = "Привет, класс.", *, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.load_calls = 0
        self.transcribe_calls = []

    def load(self) -> None:
        self.load_calls += 1

    def transcribe(self, wav_path: str, *, language: str | None, beam_size: int, vad_filter: bool) -> dict:
        self.transcribe_calls.append(
            {
                "wav_path": wav_path,
                "language": language,
                "beam_size": beam_size,
                "vad_filter": vad_filter,
            }
        )
        if self.error is not None:
            raise self.error
        return {"text": self.text, "language": language or "ru", "confidence": 0.91}


@pytest.mark.asyncio
async def test_faster_whisper_factory_creates_provider_without_loading_backend():
    backend = FakeFasterWhisperBackend()

    provider = create_stt_provider(
        "faster_whisper",
        model_path="C:/models/whisper/faster-whisper-small",
        backend=backend,
    )

    assert provider.name == "faster_whisper"
    assert backend.load_calls == 0
    assert provider.status()["status"] == "configured"


def test_faster_whisper_missing_model_path_reports_not_configured():
    provider = FasterWhisperSTTProvider(model_path="")

    status = provider.status()

    assert status["ready"] is False
    assert status["status"] == "not_configured"
    assert status["missing"] == ["FASTER_WHISPER_MODEL_PATH"]


@pytest.mark.asyncio
async def test_faster_whisper_transcribes_buffered_audio_on_commit_and_emits_final_event():
    backend = FakeFasterWhisperBackend("Откройте редактор кода.")
    provider = FasterWhisperSTTProvider(
        model_path="C:/models/whisper/faster-whisper-small",
        backend=backend,
        language="ru",
        disable_auto_language_detection=True,
        beam_size=1,
        vad_filter=False,
    )

    await provider.connect()
    await provider.send_audio(
        b"\x01\x00" * 160,
        {
            "sample_rate": 16000,
            "channels": 1,
            "format": "pcm_s16le",
            "audio_received_at": datetime.utcnow(),
            "source": "browser_ws",
            "speaker_id": "teacher",
        },
    )
    await provider.commit("teacher_force_commit")
    event = await _next_event(provider.events())

    assert event.is_final is True
    assert event.is_partial is False
    assert event.text == "Откройте редактор кода."
    assert event.language == "ru"
    assert event.provider == "faster_whisper"
    assert event.raw["stage"] == "final"
    assert backend.load_calls == 1
    assert backend.transcribe_calls[0]["language"] == "ru"
    assert backend.transcribe_calls[0]["beam_size"] == 1
    assert backend.transcribe_calls[0]["vad_filter"] is False
    assert provider.final_events_received == 1
    assert provider.last_transcript == "Откройте редактор кода."


@pytest.mark.asyncio
async def test_faster_whisper_error_is_sanitized():
    backend = FakeFasterWhisperBackend(error=RuntimeError("bad model C:/secret/models/fw/model.bin"))
    provider = FasterWhisperSTTProvider(
        model_path="C:/secret/models/fw",
        backend=backend,
    )

    await provider.connect()
    await provider.send_audio(b"\x01\x00" * 160, {"sample_rate": 16000, "channels": 1, "format": "pcm_s16le"})

    with pytest.raises(RuntimeError, match="<model_path>"):
        await provider.commit("test")

    assert "C:/secret" not in provider.status()["last_error"]


@pytest.mark.asyncio
async def test_audio_pipeline_accepts_faster_whisper_final_event_shape():
    published = []
    provider = FasterWhisperSTTProvider(
        model_path="C:/models/whisper/faster-whisper-small",
        backend=FakeFasterWhisperBackend("Привет, класс."),
    )
    pipeline = AudioPipeline(
        lesson_id="lesson_fw",
        meeting_id="meeting_fw",
        source=MockAudioSource(interval_seconds=0.01, max_chunks=0),
        stt=provider,
        translator=MockTranslator(),
        target_languages=["kk", "uz"],
        translate_partials=False,
        publish=lambda payload: _capture_async(published, payload),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: None,
    )

    await pipeline.start()
    await provider.send_audio(b"\x01\x00" * 160, {"sample_rate": 16000, "channels": 1, "format": "pcm_s16le"})
    await provider.commit("test")
    await asyncio.sleep(0.05)
    await pipeline.stop()

    final = next(item for item in published if item["is_final"])
    assert final["provider"] == {"stt": "faster_whisper", "translator": "mock"}
    assert final["caption_id"]
    assert set(final["translations"]) == {"kk", "uz"}
    assert final["original_text"] == "Привет, класс."


async def _next_event(events: AsyncIterator):
    async for event in events:
        return event
    raise AssertionError("No event emitted")


async def _capture_async(items: list, payload: dict) -> None:
    items.append(payload)
