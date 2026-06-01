import pytest

from app.realtime.caption_hub import CaptionHub


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


@pytest.mark.asyncio
async def test_caption_hub_broadcasts_only_to_lesson_clients():
    hub = CaptionHub()
    lesson_client = FakeWebSocket()
    other_client = FakeWebSocket()

    await hub.connect("lesson_1", lesson_client)
    await hub.connect("lesson_2", other_client)
    await hub.broadcast_caption("lesson_1", {"event": "caption", "lesson_id": "lesson_1"})

    assert lesson_client.messages == [{"event": "caption", "lesson_id": "lesson_1"}]
    assert other_client.messages == []
    assert hub.connected_count("lesson_1") == 1

