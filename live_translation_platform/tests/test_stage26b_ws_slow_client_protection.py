import asyncio
from types import SimpleNamespace

import pytest

from app.monitoring.metrics import RuntimeMetrics, runtime_metrics_snapshot
from app.realtime.caption_hub import CaptionHub
from app.realtime.question_hub import QuestionHub


class FastWebSocket:
    def __init__(self) -> None:
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


class SlowWebSocket:
    def __init__(self, delay_seconds: float = 1.0) -> None:
        self.delay_seconds = delay_seconds
        self.messages = []

    async def send_json(self, payload):
        await asyncio.sleep(self.delay_seconds)
        self.messages.append(payload)


class FailingWebSocket:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc or RuntimeError("websocket closed")
        self.messages = []

    async def send_json(self, payload):
        raise self.exc


@pytest.mark.asyncio
async def test_caption_hub_slow_client_times_out_without_blocking_fast_clients():
    metrics = RuntimeMetrics()
    hub = CaptionHub(
        runtime_metrics=metrics,
        send_timeout_seconds=0.05,
        max_concurrency=20,
        drop_on_timeout=True,
        metrics_enabled=True,
    )
    fast_clients = [FastWebSocket() for _ in range(25)]
    slow_client = SlowWebSocket(delay_seconds=1.0)
    for websocket in [*fast_clients, slow_client]:
        await hub.connect("lesson_1", websocket)

    started = asyncio.get_running_loop().time()
    await hub.deliver_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.25
    assert all(client.messages == [{"event": "caption", "lesson_id": "lesson_1"}] for client in fast_clients)
    assert slow_client.messages == []
    assert hub.connected_count("lesson_1") == len(fast_clients)
    assert metrics.websocket_broadcasts_total == 1
    assert metrics.websocket_send_timeouts_total == 1
    assert metrics.websocket_clients_dropped_total == 1
    assert metrics.caption_broadcast_latency_ms_avg() is not None


@pytest.mark.asyncio
async def test_caption_hub_send_error_drops_client_and_records_failure():
    metrics = RuntimeMetrics()
    hub = CaptionHub(
        runtime_metrics=metrics,
        send_timeout_seconds=0.1,
        max_concurrency=10,
        drop_on_timeout=True,
        metrics_enabled=True,
    )
    fast_client = FastWebSocket()
    failing_client = FailingWebSocket()
    await hub.connect("lesson_1", fast_client)
    await hub.connect("lesson_1", failing_client)

    await hub.deliver_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})

    assert fast_client.messages == [{"event": "caption", "lesson_id": "lesson_1"}]
    assert hub.connected_count("lesson_1") == 1
    assert metrics.websocket_send_failures_total == 1
    assert metrics.websocket_clients_dropped_total == 1


@pytest.mark.asyncio
async def test_question_hub_slow_client_times_out_without_blocking_fast_clients():
    metrics = RuntimeMetrics()
    hub = QuestionHub(
        runtime_metrics=metrics,
        send_timeout_seconds=0.05,
        max_concurrency=20,
        drop_on_timeout=True,
        metrics_enabled=True,
    )
    fast_clients = [FastWebSocket() for _ in range(20)]
    slow_client = SlowWebSocket(delay_seconds=1.0)
    for websocket in [*fast_clients, slow_client]:
        await hub.connect("lesson_1", websocket)

    started = asyncio.get_running_loop().time()
    await hub.deliver("lesson_1", {"event": "question_created", "lesson_id": "lesson_1"})
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.25
    assert all(client.messages == [{"event": "question_created", "lesson_id": "lesson_1"}] for client in fast_clients)
    assert slow_client.messages == []
    assert hub.connected_count("lesson_1") == len(fast_clients)
    assert metrics.websocket_broadcasts_total == 1
    assert metrics.websocket_send_timeouts_total == 1
    assert metrics.websocket_clients_dropped_total == 1
    assert metrics.question_broadcast_latency_ms_avg() is not None


@pytest.mark.asyncio
async def test_question_hub_send_error_drops_client_and_records_failure():
    metrics = RuntimeMetrics()
    hub = QuestionHub(
        runtime_metrics=metrics,
        send_timeout_seconds=0.1,
        max_concurrency=10,
        drop_on_timeout=True,
        metrics_enabled=True,
    )
    fast_client = FastWebSocket()
    failing_client = FailingWebSocket()
    await hub.connect("lesson_1", fast_client)
    await hub.connect("lesson_1", failing_client)

    await hub.deliver("lesson_1", {"event": "question_created", "lesson_id": "lesson_1"})

    assert fast_client.messages == [{"event": "question_created", "lesson_id": "lesson_1"}]
    assert hub.connected_count("lesson_1") == 1
    assert metrics.websocket_send_failures_total == 1
    assert metrics.websocket_clients_dropped_total == 1


def test_runtime_metrics_snapshot_includes_websocket_broadcast_fields():
    metrics = RuntimeMetrics()
    caption_hub = CaptionHub(runtime_metrics=metrics)
    question_hub = QuestionHub(runtime_metrics=metrics)
    metrics.record_websocket_broadcast("caption", 12.5)
    metrics.record_websocket_broadcast("question", 7.5)
    metrics.record_websocket_send_timeout()
    metrics.record_websocket_send_failure()
    metrics.record_websocket_client_dropped()

    app = SimpleNamespace(
        state=SimpleNamespace(
            runtime_metrics=metrics,
            session_manager=SimpleNamespace(sessions={}),
            caption_hub=caption_hub,
            question_hub=question_hub,
            rtms_manager=None,
            browser_audio_manager=None,
            provider_runtime={},
            tts_shared_cache=None,
        )
    )

    payload = runtime_metrics_snapshot(app)

    assert payload["websocket_broadcasts_total"] == 2
    assert payload["websocket_send_failures_total"] == 1
    assert payload["websocket_send_timeouts_total"] == 1
    assert payload["websocket_clients_dropped_total"] == 1
    assert payload["caption_broadcast_latency_ms_avg"] == 12.5
    assert payload["question_broadcast_latency_ms_avg"] == 7.5
