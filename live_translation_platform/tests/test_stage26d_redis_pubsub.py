import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.infra.pubsub import PubSubStatus, RedisPubSubFanout
from app.main import create_app
from app.monitoring.metrics import RuntimeMetrics, runtime_metrics_snapshot
from app.realtime.caption_hub import CaptionHub
from app.realtime.question_hub import QuestionHub


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


class FakeRedis:
    def __init__(self, fail_publish: bool = False) -> None:
        self.fail_publish = fail_publish
        self.published = []
        self.closed = False

    async def publish(self, channel: str, message: str) -> int:
        if self.fail_publish:
            raise RuntimeError("redis publish failed")
        self.published.append((channel, json.loads(message)))
        return 1

    async def aclose(self) -> None:
        self.closed = True


class TimeoutPubSub:
    async def psubscribe(self, pattern: str) -> None:
        self.pattern = pattern

    async def listen(self):
        raise TimeoutError("Timeout reading from socket")
        yield


@pytest.mark.asyncio
async def test_redis_disabled_caption_and_question_local_broadcasts_work():
    caption_hub = CaptionHub()
    question_hub = QuestionHub()
    caption_client = FakeWebSocket()
    question_client = FakeWebSocket()

    await caption_hub.connect("lesson_1", caption_client)
    await question_hub.connect("lesson_1", question_client)
    await caption_hub.broadcast_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})
    await question_hub.broadcast("lesson_1", {"event": "question_created", "lesson_id": "lesson_1"})

    assert caption_client.messages == [{"event": "caption", "lesson_id": "lesson_1"}]
    assert question_client.messages == [{"event": "question_created", "lesson_id": "lesson_1"}]


@pytest.mark.asyncio
async def test_fake_redis_pubsub_caption_publish_receive_and_metrics():
    metrics = RuntimeMetrics()
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis()
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a", runtime_metrics=metrics)
    hub = CaptionHub(pubsub=fanout, runtime_metrics=metrics)
    fanout.register_caption_handler(hub.deliver_caption)
    client = FakeWebSocket()

    await hub.connect("lesson_1", client)
    await hub.broadcast_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1", "text": "hello"})

    assert client.messages == []
    channel, envelope = redis.published[0]
    assert channel == "live_translation:lesson:lesson_1:captions"
    assert metrics.redis_pubsub_messages_published_total == 1

    await fanout.dispatch_message(channel, json.dumps(envelope))

    assert client.messages == [{"event": "caption", "lesson_id": "lesson_1", "text": "hello"}]
    assert metrics.redis_pubsub_messages_received_total == 1
    assert metrics.redis_pubsub_latency_ms_avg() is not None


@pytest.mark.asyncio
async def test_pubsub_idle_listener_timeout_does_not_poison_future_publish():
    metrics = RuntimeMetrics()
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis()
    redis.pubsub = lambda: TimeoutPubSub()
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a", runtime_metrics=metrics)

    task = asyncio.create_task(fanout._listen())
    await asyncio.sleep(0.01)
    published = await fanout.publish_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert published is True
    assert metrics.redis_pubsub_errors_total == 0
    assert metrics.redis_pubsub_messages_published_total == 1
    assert fanout.status.error is None


@pytest.mark.asyncio
async def test_fake_redis_pubsub_question_publish_receive_and_metrics():
    metrics = RuntimeMetrics()
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis()
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a", runtime_metrics=metrics)
    hub = QuestionHub(pubsub=fanout, runtime_metrics=metrics)
    fanout.register_question_handler(hub.deliver)
    client = FakeWebSocket()

    await hub.connect("lesson_1", client)
    await hub.broadcast("lesson_1", {"event": "question_created", "lesson_id": "lesson_1"})
    channel, envelope = redis.published[0]
    await fanout.dispatch_message(channel, json.dumps(envelope))

    assert channel == "live_translation:lesson:lesson_1:questions"
    assert client.messages == [{"event": "question_created", "lesson_id": "lesson_1"}]
    assert metrics.redis_pubsub_messages_published_total == 1
    assert metrics.redis_pubsub_messages_received_total == 1


@pytest.mark.asyncio
async def test_origin_worker_receives_own_pubsub_event_without_duplicate_local_delivery():
    metrics = RuntimeMetrics()
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis()
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a", runtime_metrics=metrics)
    hub = CaptionHub(pubsub=fanout, runtime_metrics=metrics)
    fanout.register_caption_handler(hub.deliver_caption)
    client = FakeWebSocket()

    await hub.connect("lesson_1", client)
    await hub.broadcast_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})
    channel, envelope = redis.published[0]
    await fanout.dispatch_message(channel, json.dumps(envelope))

    assert client.messages == [{"event": "caption", "lesson_id": "lesson_1"}]


@pytest.mark.asyncio
async def test_redis_publish_error_increments_metrics_and_falls_back_to_local_delivery():
    metrics = RuntimeMetrics()
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis(fail_publish=True)
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a", runtime_metrics=metrics)
    hub = CaptionHub(pubsub=fanout, runtime_metrics=metrics)
    client = FakeWebSocket()

    await hub.connect("lesson_1", client)
    await hub.broadcast_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})

    assert client.messages == [{"event": "caption", "lesson_id": "lesson_1"}]
    assert metrics.redis_pubsub_errors_total == 1
    assert fanout.status.connected is False
    assert fanout.status.error == "redis publish failed"


def test_runtime_metrics_snapshot_includes_redis_pubsub_fields():
    metrics = RuntimeMetrics()
    metrics.record_redis_pubsub_published(2.5)
    metrics.record_redis_pubsub_received(3.5)
    metrics.record_redis_pubsub_error()
    app = _metrics_app(metrics, redis_status={"enabled": True, "connected": True})

    payload = runtime_metrics_snapshot(app)

    assert payload["redis_enabled"] is True
    assert payload["redis_connected"] is True
    assert payload["redis_pubsub_enabled"] is True
    assert payload["redis_pubsub_messages_published_total"] == 1
    assert payload["redis_pubsub_messages_received_total"] == 1
    assert payload["redis_pubsub_errors_total"] == 1
    assert payload["redis_pubsub_latency_ms_avg"] == 3.0


def test_redis_health_redacts_password_when_ping_fails(tmp_path, monkeypatch):
    async def fake_create(settings):
        return FakeRedis()

    async def fake_ping(client, timeout):
        raise RuntimeError("bad password redis://user:secret-pass@redis:6379/0")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-redact.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://user:secret-pass@redis:6379/0")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    monkeypatch.setattr("app.infra.redis.ping_redis", fake_ping)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["redis"]["connected"] is False
    assert "secret-pass" not in response.text
    assert "redis://user:secret-pass@redis:6379/0" not in response.text


def test_production_required_redis_unavailable_fails_readiness(tmp_path, monkeypatch):
    async def fake_create(settings):
        return FakeRedis()

    async def fake_ping(client, timeout):
        raise TimeoutError("redis unavailable")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-required.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("POSTGRES_REQUIRED_IN_PRODUCTION", "false")
    monkeypatch.setenv("SQLITE_ALLOWED_IN_PRODUCTION", "true")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.test")
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("ENABLE_OPENAPI_DOCS", "false")
    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "false")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("WEBSOCKET_AUTH_REQUIRED_IN_PRODUCTION", "false")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION", "false")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_REQUIRED_IN_PRODUCTION", "true")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    monkeypatch.setattr("app.infra.redis.ping_redis", fake_ping)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "not_ready"
    assert "REDIS_AVAILABLE" in response.json()["config_missing"]


def test_pubsub_fail_closed_degraded_status_fails_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'pubsub-fail-closed.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_PUBSUB_ENABLED", "true")
    monkeypatch.setenv("REDIS_PUBSUB_FAIL_CLOSED", "true")
    app = create_app()
    app.state.pubsub_status = PubSubStatus(enabled=True, connected=False, error="pubsub unavailable")

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "not_ready"
    assert "REDIS_PUBSUB_AVAILABLE" in response.json()["config_missing"]


def _metrics_app(metrics: RuntimeMetrics, redis_status: dict):
    class State:
        pass

    state = State()
    state.runtime_metrics = metrics
    state.settings = Settings(redis_enabled=True, redis_pubsub_enabled=True)
    state.redis_status = type("RedisStatus", (), {"to_dict": lambda self: redis_status})()
    state.session_manager = type("SessionManager", (), {"sessions": {}})()
    state.caption_hub = CaptionHub(runtime_metrics=metrics)
    state.question_hub = QuestionHub(runtime_metrics=metrics)
    state.rtms_manager = None
    state.browser_audio_manager = None
    state.provider_runtime = {}
    state.tts_shared_cache = None
    return type("App", (), {"state": state})()
