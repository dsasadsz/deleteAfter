import json

import pytest

from app.config import Settings
from app.infra.pubsub import RedisPubSubFanout
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

    async def publish(self, channel: str, message: str) -> int:
        if self.fail_publish:
            raise RuntimeError("redis disconnected")
        self.published.append((channel, json.loads(message)))
        return 1


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
async def test_fake_pubsub_delivers_caption_event_to_local_subscriber():
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis()
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a")
    hub = CaptionHub(pubsub=fanout)
    fanout.register_caption_handler(hub.deliver_caption)
    client = FakeWebSocket()

    await hub.connect("lesson_1", client)
    await hub.broadcast_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1", "text": "hello"})

    assert client.messages == []
    channel, envelope = redis.published[0]
    assert channel == "live_translation:lesson:lesson_1:captions"
    await fanout.dispatch_message(channel, json.dumps(envelope))

    assert client.messages == [{"event": "caption", "lesson_id": "lesson_1", "text": "hello"}]


@pytest.mark.asyncio
async def test_fake_pubsub_delivers_question_event_to_local_subscriber():
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis()
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a")
    hub = QuestionHub(pubsub=fanout)
    fanout.register_question_handler(hub.deliver)
    client = FakeWebSocket()

    await hub.connect("lesson_1", client)
    await hub.broadcast("lesson_1", {"event": "question_created", "lesson_id": "lesson_1"})

    channel, envelope = redis.published[0]
    assert channel == "live_translation:lesson:lesson_1:questions"
    await fanout.dispatch_message(channel, json.dumps(envelope))

    assert client.messages == [{"event": "question_created", "lesson_id": "lesson_1"}]


@pytest.mark.asyncio
async def test_pubsub_caption_path_does_not_duplicate_same_worker_delivery():
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis()
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a")
    hub = CaptionHub(pubsub=fanout)
    fanout.register_caption_handler(hub.deliver_caption)
    client = FakeWebSocket()

    await hub.connect("lesson_1", client)
    await hub.broadcast_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})
    channel, envelope = redis.published[0]
    await fanout.dispatch_message(channel, json.dumps(envelope))

    assert client.messages == [{"event": "caption", "lesson_id": "lesson_1"}]


@pytest.mark.asyncio
async def test_pubsub_publish_error_falls_back_to_local_caption_delivery():
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis(fail_publish=True)
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a")
    hub = CaptionHub(pubsub=fanout)
    client = FakeWebSocket()

    await hub.connect("lesson_1", client)
    await hub.broadcast_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})

    assert client.messages == [{"event": "caption", "lesson_id": "lesson_1"}]
    assert fanout.status.connected is False
    assert fanout.status.error == "redis disconnected"


@pytest.mark.asyncio
async def test_pubsub_listener_error_falls_back_to_local_caption_delivery():
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis()
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a")
    hub = CaptionHub(pubsub=fanout)
    client = FakeWebSocket()
    fanout.status.connected = False
    fanout.status.error = "listener disconnected"

    await hub.connect("lesson_1", client)
    await hub.broadcast_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})

    assert client.messages == [{"event": "caption", "lesson_id": "lesson_1"}]
    assert redis.published == []


@pytest.mark.asyncio
async def test_debug_and_diagnostics_channels_are_supported():
    settings = Settings(redis_prefix="live_translation", redis_pubsub_enabled=True)
    redis = FakeRedis()
    fanout = RedisPubSubFanout(settings, redis, origin_worker_id="worker_a")
    hub = CaptionHub(pubsub=fanout)
    fanout.register_debug_handler(hub.deliver_debug)
    debug_client = FakeWebSocket()

    await hub.connect("lesson_1", debug_client, debug=True)
    await hub.broadcast_debug("lesson_1", {"event": "debug", "lesson_id": "lesson_1"})
    channel, envelope = redis.published[0]

    assert channel == "live_translation:lesson:lesson_1:debug"
    await fanout.dispatch_message(channel, json.dumps(envelope))

    assert debug_client.messages == [{"event": "debug", "lesson_id": "lesson_1"}]
    assert fanout.channel("lesson_1", "diagnostics") == "live_translation:lesson:lesson_1:diagnostics"
