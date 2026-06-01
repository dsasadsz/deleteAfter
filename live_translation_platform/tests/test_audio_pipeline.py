import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest

from app.audio.base import AudioChunk, AudioSource
from app.audio.mock_audio_source import MockAudioSource
from app.realtime.audio_pipeline import AudioPipeline
from app.stt.base import STTEvent
from app.stt.mock_stt import MockSTT
from app.translation.mock_translator import MockTranslator


@pytest.mark.asyncio
async def test_audio_pipeline_broadcasts_final_caption_with_latency():
    events = []

    async def publish(payload):
        events.append(payload)

    pipeline = AudioPipeline(
        lesson_id="lesson_test",
        meeting_id="123456789",
        source=MockAudioSource(interval_seconds=0.01, max_chunks=1),
        stt=MockSTT(),
        translator=MockTranslator(),
        target_languages=["kk", "uz", "zh-Hans"],
        translate_partials=False,
        publish=publish,
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: None,
    )

    await pipeline.start()
    await asyncio.sleep(0.2)
    await pipeline.stop()

    final_events = [event for event in events if event["is_final"]]
    assert final_events
    assert set(final_events[0]["translations"]) == {"kk", "uz", "zh-Hans"}
    assert final_events[0]["latency_ms"]["total"] >= final_events[0]["latency_ms"]["stt"]


@pytest.mark.asyncio
async def test_audio_pipeline_drops_oldest_when_queue_overflows():
    debug_events = []
    stt = SlowSTTProvider()
    pipeline = AudioPipeline(
        lesson_id="lesson_drop_oldest",
        meeting_id="123",
        source=BurstAudioSource(total_chunks=6),
        stt=stt,
        translator=MockTranslator(),
        target_languages=["kk"],
        translate_partials=False,
        publish=lambda payload: _noop_async(),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: _capture_async(debug_events, payload),
        queue_max_size=1,
        drop_policy="drop_oldest",
    )

    await pipeline.start()
    await asyncio.sleep(0.08)
    await pipeline.stop()

    assert pipeline.pipeline_chunks_dropped > 0
    assert pipeline.pipeline_backpressure_events == pipeline.pipeline_chunks_dropped
    assert pipeline.queue.qsize() <= 1
    assert any(event["code"] == "pipeline_backpressure" and event["payload"]["drop_policy"] == "drop_oldest" for event in debug_events)


@pytest.mark.asyncio
async def test_audio_pipeline_drops_newest_when_queue_overflows():
    debug_events = []
    stt = SlowSTTProvider()
    pipeline = AudioPipeline(
        lesson_id="lesson_drop_newest",
        meeting_id="123",
        source=BurstAudioSource(total_chunks=6),
        stt=stt,
        translator=MockTranslator(),
        target_languages=["kk"],
        translate_partials=False,
        publish=lambda payload: _noop_async(),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: _capture_async(debug_events, payload),
        queue_max_size=1,
        drop_policy="drop_newest",
    )

    await pipeline.start()
    await asyncio.sleep(0.08)
    await pipeline.stop()

    assert pipeline.pipeline_chunks_dropped > 0
    assert pipeline.pipeline_backpressure_events == pipeline.pipeline_chunks_dropped
    assert pipeline.queue.qsize() <= 1
    assert any(event["code"] == "pipeline_backpressure" and event["payload"]["drop_policy"] == "drop_newest" for event in debug_events)


@pytest.mark.asyncio
async def test_stt_disconnect_degrades_pipeline_without_unbounded_queue_growth():
    debug_events = []
    pipeline_events = []
    pipeline = AudioPipeline(
        lesson_id="lesson_stt_disconnect",
        meeting_id="123",
        source=BurstAudioSource(total_chunks=20, interval_seconds=0.005),
        stt=DisconnectingSTTProvider(),
        translator=MockTranslator(),
        target_languages=["kk"],
        translate_partials=False,
        publish=lambda payload: _noop_async(),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: _capture_async(debug_events, payload),
        on_pipeline_event=lambda event, payload: pipeline_events.append((event, payload)),
        queue_max_size=2,
        drop_policy="drop_newest",
    )

    await pipeline.start()
    await asyncio.sleep(0.12)
    await pipeline.stop()

    assert pipeline.status in {"degraded", "error", "stopped"}
    assert pipeline.error_classification == "disconnected"
    assert pipeline.queue.qsize() <= 2
    assert any(event["code"] == "stt_disconnected" for event in debug_events)
    assert any(event == "stt_disconnected" for event, _ in pipeline_events)


@pytest.mark.asyncio
async def test_unsupported_commit_provider_logs_warning_and_continues():
    debug_events = []
    pipeline = AudioPipeline(
        lesson_id="lesson_unsupported_commit",
        meeting_id="123",
        source=CommitOnlySource(),
        stt=UnsupportedCommitSTTProvider(),
        translator=MockTranslator(),
        target_languages=["kk"],
        translate_partials=False,
        publish=lambda payload: _noop_async(),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: _capture_async(debug_events, payload),
        queue_max_size=2,
    )

    await pipeline.start()
    await asyncio.sleep(0.05)
    await pipeline.stop()

    assert pipeline.status == "stopped"
    assert any(event["code"] == "stt_commit_unsupported" for event in debug_events)


@pytest.mark.asyncio
async def test_audio_pipeline_skips_duplicate_final_caption_within_dedupe_window():
    published = []
    saved_captions = []
    saved_metrics = []
    debug_events = []
    pipeline = AudioPipeline(
        lesson_id="lesson_duplicate_final",
        meeting_id="123",
        source=MockAudioSource(interval_seconds=0.01, max_chunks=0),
        stt=MockSTT(),
        translator=MockTranslator(),
        target_languages=["kk"],
        translate_partials=False,
        publish=lambda payload: _capture_async(published, payload),
        save_caption=lambda payload: saved_captions.append(payload),
        save_metric=lambda payload: saved_metrics.append(payload),
        publish_debug=lambda payload: _capture_async(debug_events, payload),
    )
    timestamp = datetime.utcnow()
    event = STTEvent(
        text="repeat me",
        is_partial=False,
        is_final=True,
        language="ru-RU",
        confidence=None,
        provider="mock",
        timestamp=timestamp,
        speaker_id="teacher",
        raw={"event_id": "provider-final-1"},
        audio_received_at=timestamp,
    )

    await pipeline._handle_event(event)
    await pipeline._handle_event(
        STTEvent(
            text="  repeat   me  ",
            is_partial=False,
            is_final=True,
            language="ru-RU",
            confidence=None,
            provider="mock",
            timestamp=timestamp + timedelta(seconds=1),
            speaker_id="teacher",
            raw={"event_id": "provider-final-duplicate"},
            audio_received_at=timestamp,
        )
    )

    assert len(published) == 1
    assert len(saved_captions) == 1
    assert len(saved_metrics) == 1
    assert published[0]["caption_id"]
    assert published[0]["sequence"] == 1
    assert published[0]["text_hash"]
    assert published[0]["provider_event_id"] == "provider-final-1"
    assert any(event["code"] == "duplicate_final_caption_skipped" for event in debug_events)


@pytest.mark.asyncio
async def test_audio_pipeline_allows_same_final_caption_after_dedupe_window():
    published = []
    saved_captions = []
    pipeline = AudioPipeline(
        lesson_id="lesson_repeat_after_gap",
        meeting_id="123",
        source=MockAudioSource(interval_seconds=0.01, max_chunks=0),
        stt=MockSTT(),
        translator=MockTranslator(),
        target_languages=["kk"],
        translate_partials=False,
        publish=lambda payload: _capture_async(published, payload),
        save_caption=lambda payload: saved_captions.append(payload),
        save_metric=lambda payload: None,
        publish_debug=lambda payload: None,
    )
    timestamp = datetime.utcnow()

    for offset_seconds in (0, 10):
        await pipeline._handle_event(
            STTEvent(
                text="real repeated phrase",
                is_partial=False,
                is_final=True,
                language="ru-RU",
                confidence=None,
                provider="mock",
                timestamp=timestamp + timedelta(seconds=offset_seconds),
                speaker_id="teacher",
                raw={},
                audio_received_at=timestamp + timedelta(seconds=offset_seconds),
            )
        )

    assert len(published) == 2
    assert len(saved_captions) == 2
    assert [event["sequence"] for event in published] == [1, 2]
    assert published[0]["caption_id"] != published[1]["caption_id"]


class BurstAudioSource(AudioSource):
    name = "burst"

    def __init__(self, total_chunks: int, interval_seconds: float = 0.0) -> None:
        self.total_chunks = total_chunks
        self.interval_seconds = interval_seconds
        self._closed = False

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        for index in range(self.total_chunks):
            if self._closed:
                break
            yield AudioChunk(
                data=f"chunk-{index}".encode(),
                received_at=datetime.utcnow(),
                source=self.name,
                metadata={"text": f"chunk {index}", "sequence": index},
            )
            if self.interval_seconds:
                await asyncio.sleep(self.interval_seconds)

    async def close(self) -> None:
        self._closed = True


class CommitOnlySource(AudioSource):
    name = "commit_only"

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        yield AudioChunk(
            data=b"",
            received_at=datetime.utcnow(),
            source=self.name,
            metadata={"control": "stt_commit", "reason": "max_segment_duration"},
        )

    async def close(self) -> None:
        return None


class SlowSTTProvider:
    name = "slow"
    supports_commit = True

    def __init__(self) -> None:
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()

    async def connect(self):
        return None

    async def send_audio(self, audio_chunk, metadata=None):
        await asyncio.sleep(0.2)

    async def commit(self, reason: str | None = None):
        return None

    async def events(self):
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event

    async def close(self):
        await self._queue.put(None)


class DisconnectingSTTProvider(SlowSTTProvider):
    name = "disconnecting"

    async def send_audio(self, audio_chunk, metadata=None):
        raise ConnectionError("provider websocket disconnected")


class UnsupportedCommitSTTProvider(SlowSTTProvider):
    name = "unsupported_commit"
    supports_commit = False


async def _capture_async(items, item):
    items.append(item)


async def _noop_async():
    return None
