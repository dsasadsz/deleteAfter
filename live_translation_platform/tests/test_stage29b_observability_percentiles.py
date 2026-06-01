from types import SimpleNamespace
from datetime import datetime

import pytest

from app.monitoring.metrics import RollingLatencyTracker, RuntimeMetrics, runtime_metrics_snapshot
from app.audio.mock_audio_source import MockAudioSource
from app.api.tts import _record_runtime_provider_error
from app.realtime.audio_pipeline import AudioPipeline
from app.realtime.lesson_session import LessonSessionManager
from app.stt.base import STTEvent
from app.stt.mock_stt import MockSTT
from scripts import load_test_6_lessons_1000_students as ws_load_test


def test_rolling_latency_tracker_computes_percentiles_and_average():
    tracker = RollingLatencyTracker(maxlen=1000)
    for value in range(1, 101):
        tracker.record(value)

    assert tracker.count == 100
    assert tracker.avg == 50.5
    assert tracker.p50 == 50.5
    assert tracker.p95 == 95.05
    assert tracker.p99 == 99.01


def test_rolling_latency_tracker_empty_returns_none_percentiles():
    tracker = RollingLatencyTracker(maxlen=10)

    assert tracker.count == 0
    assert tracker.avg is None
    assert tracker.p50 is None
    assert tracker.p95 is None
    assert tracker.p99 is None


def test_rolling_latency_tracker_is_bounded_and_numeric_only():
    tracker = RollingLatencyTracker(maxlen=3)
    for value in (1, 2, 3, 4, None, "not-numeric"):
        tracker.record(value)

    assert tracker.count == 3
    assert list(tracker.values) == [2.0, 3.0, 4.0]
    assert tracker.p50 == 3.0


def test_runtime_metrics_snapshot_exposes_bounded_latency_percentiles():
    metrics = RuntimeMetrics()
    for latency_ms in range(1, 601):
        metrics.record_websocket_broadcast("caption", latency_ms)
        metrics.record_websocket_broadcast("question", latency_ms)
        metrics.record_redis_pubsub_received(latency_ms)
        metrics.record_tts_request(latency_ms)
        metrics.record_stt_latency(latency_ms)
        metrics.record_translation_latency(latency_ms)

    payload = runtime_metrics_snapshot(_metrics_app(metrics))

    for prefix in (
        "caption_broadcast_latency_ms",
        "question_broadcast_latency_ms",
        "redis_pubsub_latency_ms",
        "stt_latency_ms",
        "tts_latency_ms",
        "translation_latency_ms",
    ):
        assert payload[f"{prefix}_p50"] == 350.5
        assert payload[f"{prefix}_p95"] == 575.05
        assert payload[f"{prefix}_p99"] == 595.01


def test_runtime_metrics_snapshot_returns_none_for_empty_percentiles():
    payload = runtime_metrics_snapshot(_metrics_app(RuntimeMetrics()))

    for key in (
        "caption_broadcast_latency_ms_p50",
        "caption_broadcast_latency_ms_p95",
        "caption_broadcast_latency_ms_p99",
        "question_broadcast_latency_ms_p50",
        "redis_pubsub_latency_ms_p50",
        "translation_latency_ms_p50",
        "tts_latency_ms_p50",
        "stt_latency_ms_p95",
    ):
        assert key in payload
        assert payload[key] is None


def test_runtime_metrics_snapshot_exposes_provider_error_and_timeout_counters_by_provider():
    metrics = RuntimeMetrics()
    metrics.record_provider_error("azure")
    metrics.record_provider_error("azure", "429 too many requests")
    metrics.record_provider_error("elevenlabs")
    metrics.record_provider_error("cartesia", "401 unauthorized")
    metrics.record_provider_timeout("azure")

    payload = runtime_metrics_snapshot(_metrics_app(metrics))

    assert payload["provider_errors_total"] == 4
    assert payload["provider_errors_by_provider"] == {"azure": 2, "elevenlabs": 1, "cartesia": 1}
    assert payload["provider_timeouts_by_provider"] == {"azure": 1}
    assert payload["provider_timeout_errors_total"] == 1
    assert payload["provider_rate_limit_errors_total"] == 1
    assert payload["provider_auth_errors_total"] == 1
    assert payload["provider_unknown_errors_total"] == 2


def test_provider_counters_bucket_unknown_labels_to_keep_memory_bounded():
    metrics = RuntimeMetrics()
    for index in range(25):
        metrics.record_provider_error(f"user-supplied-provider-{index}-secret-token-value")

    payload = runtime_metrics_snapshot(_metrics_app(metrics))

    assert payload["provider_errors_total"] == 25
    assert payload["provider_errors_by_provider"] == {"other": 25}


def test_lesson_session_records_provider_error_and_timeout_by_provider():
    metrics = RuntimeMetrics()
    manager = LessonSessionManager.__new__(LessonSessionManager)
    manager.runtime_metrics = metrics

    manager._record_runtime_metric("provider_error", {"provider": "azure", "error": "provider timed out"})

    assert metrics.provider_errors_by_provider == {"azure": 1}
    assert metrics.provider_timeouts_by_provider == {"azure": 1}


def test_lesson_session_records_empty_timeout_exception_by_error_class():
    metrics = RuntimeMetrics()
    manager = LessonSessionManager.__new__(LessonSessionManager)
    manager.runtime_metrics = metrics

    manager._record_runtime_metric("provider_error", {"provider": "azure", "error": "", "error_class": "TimeoutError"})

    assert metrics.provider_errors_by_provider == {"azure": 1}
    assert metrics.provider_timeouts_by_provider == {"azure": 1}


def test_tts_runtime_provider_error_records_timeout_by_provider():
    metrics = RuntimeMetrics()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(runtime_metrics=metrics)))

    _record_runtime_provider_error(request, "elevenlabs", TimeoutError())

    assert metrics.provider_errors_by_provider == {"elevenlabs": 1}
    assert metrics.provider_timeouts_by_provider == {"elevenlabs": 1}


@pytest.mark.asyncio
async def test_audio_pipeline_records_translation_provider_errors_by_provider():
    events = []
    pipeline = AudioPipeline(
        lesson_id="lesson_stage29b_translation",
        meeting_id="meeting_stage29b",
        source=MockAudioSource(interval_seconds=0.01, max_chunks=0),
        stt=MockSTT(),
        translator=TimeoutTranslator(),
        target_languages=["kk"],
        translate_partials=False,
        publish=lambda payload: _capture_async([], payload),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: _capture_async([], payload),
        on_pipeline_event=lambda event, payload: events.append((event, payload)),
    )

    await pipeline._handle_event(
        STTEvent(
            text="caption",
            is_partial=False,
            is_final=True,
            language="ru-RU",
            confidence=None,
            provider="mock",
            timestamp=datetime.utcnow(),
            speaker_id="teacher",
            raw={},
            audio_received_at=datetime.utcnow(),
        )
    )

    assert ("provider_error", {"provider": "azure", "error": "", "error_class": "TimeoutError"}) in events


@pytest.mark.asyncio
async def test_audio_pipeline_records_stt_provider_errors_by_provider():
    events = []
    pipeline = AudioPipeline(
        lesson_id="lesson_stage29b_stt",
        meeting_id="meeting_stage29b",
        source=MockAudioSource(interval_seconds=0.01, max_chunks=0),
        stt=TimeoutSTT(),
        translator=TimeoutTranslator(),
        target_languages=["kk"],
        translate_partials=False,
        publish=lambda payload: _capture_async([], payload),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: _capture_async([], payload),
        on_pipeline_event=lambda event, payload: events.append((event, payload)),
    )

    await pipeline._handle_stt_failure(TimeoutError())

    assert ("provider_error", {"provider": "azure", "error": "", "error_class": "TimeoutError"}) in events


def test_ws_load_test_runtime_metric_filter_keeps_percentile_observability_fields():
    filtered = ws_load_test.filter_runtime_metrics(
        {
            "caption_broadcast_latency_ms_avg": 10,
            "caption_broadcast_latency_ms_p50": 20,
            "caption_broadcast_latency_ms_p95": 25,
            "caption_broadcast_latency_ms_p99": 40,
            "redis_pubsub_latency_ms_avg": 5,
            "redis_pubsub_latency_ms_p50": 7,
            "redis_pubsub_latency_ms_p95": 8,
            "redis_pubsub_latency_ms_p99": 12,
            "provider_errors_by_provider": {"azure": 1},
            "provider_timeouts_by_provider": {"azure": 1},
            "provider_timeout_errors_total": 1,
        }
    )

    assert filtered["caption_broadcast_latency_ms_p50"] == 20
    assert filtered["caption_broadcast_latency_ms_p95"] == 25
    assert filtered["caption_broadcast_latency_ms_p99"] == 40
    assert filtered["redis_pubsub_latency_ms_p50"] == 7
    assert filtered["redis_pubsub_latency_ms_p95"] == 8
    assert filtered["redis_pubsub_latency_ms_p99"] == 12
    assert filtered["provider_errors_by_provider"] == {"azure": 1}
    assert filtered["provider_timeouts_by_provider"] == {"azure": 1}
    assert filtered["provider_timeout_errors_total"] == 1


def test_ws_load_test_client_latency_samples_are_bounded():
    client = ws_load_test.ClientStats(client_id=1, lesson_id="lesson_1")
    for latency_ms in range(600):
        client.add_latency(float(latency_ms))

    assert len(client.latencies_ms) == 500
    assert client.latencies_ms[0] == 100.0


def test_docs_describe_rolling_percentiles_and_no_raw_payload_storage():
    docs = "\n".join(
        [
            (ws_load_test.ROOT / "docs" / "load-testing.md").read_text(encoding="utf-8"),
            (ws_load_test.ROOT / "docs" / "production.md").read_text(encoding="utf-8"),
            (ws_load_test.ROOT / "docs" / "websocket-fanout-bottleneck-investigation.md").read_text(encoding="utf-8"),
        ]
    )

    assert "rolling" in docs
    assert "p95/p99" in docs
    assert "process-local" in docs
    assert "raw caption text" in docs
    assert "audio bytes" in docs
    assert "tokens" in docs


def test_analyzer_hints_for_runtime_p95_patterns():
    from scripts import analyze_6_lessons_1000_ws_report as analyzer

    args = __import__("argparse").Namespace(
        require_final_result_pass=False,
        max_p95_latency_ms=1000,
        max_p99_latency_ms=2000,
        min_receive_rate=0.99,
        max_disconnect_rate=0.01,
        max_error_count=0,
        max_ws_timeout_count=0,
    )
    report = {
        "overall_result": "fail",
        "mock_only": True,
        "real_provider_proof": False,
        "lessons": 6,
        "students": 600,
        "per_lesson_results": [{"lesson_id": "lesson_1", "students_connected": 600, "captions_published": 1, "total_received": 600, "receive_rate": 1.0}],
        "aggregate_results": {
            "students_connected": 600,
            "receive_rate": 1.0,
            "p95_caption_latency_ms": 1200,
            "p99_caption_latency_ms": 1500,
            "disconnects": 0,
            "errors": 0,
        },
        "runtime_metrics_before": {
            "redis_pubsub_messages_published_total": 0,
            "redis_pubsub_messages_received_total": 0,
            "redis_pubsub_errors_total": 0,
        },
        "runtime_metrics_after": {
            "redis_pubsub_messages_published_total": 10,
            "redis_pubsub_messages_received_total": 10,
            "redis_pubsub_errors_total": 0,
            "caption_broadcast_latency_ms_p95": 25,
            "redis_pubsub_latency_ms_p95": 150,
        },
        "runtime_metrics_during": [],
        "errors_sanitized": [],
    }

    result = analyzer.analyze_report(report, args)

    assert any("client receive p95 is high while server broadcast p95 is low" in hint for hint in result["bottleneck_hints"])
    assert any("Redis Pub/Sub p95" in hint for hint in result["bottleneck_hints"])


class TimeoutTranslator:
    name = "azure"

    async def translate_many(self, text, source_language, target_languages):
        raise TimeoutError()


class TimeoutSTT:
    name = "azure"


async def _capture_async(items, item):
    items.append(item)


def _metrics_app(metrics: RuntimeMetrics):
    state = SimpleNamespace(
        runtime_metrics=metrics,
        session_manager=SimpleNamespace(sessions={}),
        caption_hub=SimpleNamespace(_caption_clients={}, _debug_clients={}),
        question_hub=SimpleNamespace(_clients={}),
        rtms_manager=SimpleNamespace(audio_queues={}),
        browser_audio_manager=SimpleNamespace(queues={}, chunks_dropped_total=0),
        provider_runtime={},
        tts_shared_cache=None,
        settings=SimpleNamespace(redis_enabled=True, redis_pubsub_enabled=True),
        redis_status=SimpleNamespace(to_dict=lambda: {"connected": True}),
        rate_limiter=object(),
    )
    return SimpleNamespace(state=state)
