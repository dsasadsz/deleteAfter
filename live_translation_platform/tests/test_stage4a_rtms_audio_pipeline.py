import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.audio.zoom_rtms_audio_source import ZoomRTMSAudioSource
from app.main import create_app
from app.stt.mock_stt import MockSTT


@pytest.mark.asyncio
async def test_zoom_rtms_audio_source_yields_chunks_and_counters():
    queue: asyncio.Queue = asyncio.Queue()
    source = ZoomRTMSAudioSource("lesson_1", queue)
    await queue.put(
        {
            "kind": "audio",
            "data": b"abc",
            "timestamp": datetime.utcnow(),
            "metadata": {"sample_rate": 16000, "channels": 1, "format": "L16"},
        }
    )

    chunk = await anext(source.chunks())

    assert chunk.data == b"abc"
    assert chunk.lesson_id == "lesson_1"
    assert chunk.source == "zoom_rtms"
    assert chunk.sample_rate == 16000
    assert chunk.channels == 1
    assert chunk.format == "L16"
    assert source.chunks_received_from_rtms == 1
    assert source.chunks_yielded_to_pipeline == 1


@pytest.mark.asyncio
async def test_mock_stt_audio_driven_mode_emits_partial_and_final_after_chunks():
    stt = MockSTT(audio_driven=True, chunks_per_partial=2, chunks_per_final=4, min_final_interval_ms=0)
    await stt.connect()
    for index in range(4):
        await stt.send_audio(b"\0" * 320, {"audio_received_at": datetime.utcnow(), "source": "zoom_rtms", "sequence": index})

    events = []
    async for event in stt.events():
        events.append(event)
        if event.is_final:
            break

    assert any(event.is_partial for event in events)
    assert events[-1].is_final
    assert events[-1].text == "Сегодня мы изучим переменные в C#."
    assert events[-1].raw["audio_driven"] is True


def test_debug_inject_rtms_audio_drives_captions_for_zoom_lesson(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'stage4a.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("RTMS_EXPERIMENTAL_ENABLED", "true")
    monkeypatch.setenv("RTMS_PROCESS_AUDIO", "true")
    monkeypatch.setenv("MOCK_STT_AUDIO_DRIVEN", "true")
    monkeypatch.setenv("MOCK_STT_CHUNKS_PER_PARTIAL", "2")
    monkeypatch.setenv("MOCK_STT_CHUNKS_PER_FINAL", "4")
    monkeypatch.setenv("MOCK_STT_MIN_FINAL_INTERVAL_MS", "0")

    app = create_app()
    with TestClient(app) as client:
        app.state.zoom_api_client = _FakeZoomClient()
        lesson = client.post(
            "/api/lessons",
            json={
                "title": "Stage 4A zoom",
                "mode": "zoom",
                "audio_source": "zoom_rtms",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk", "uz", "zh-Hans"],
            },
        ).json()
        client.post(
            "/api/zoom/webhook",
            json={
                "event": "meeting.rtms_started",
                "payload": {
                    "rtms_stream_id": "stream_stage4a",
                    "object": {"id": lesson["zoom"]["meeting_id"], "uuid": lesson["zoom"]["meeting_uuid"]},
                },
            },
        )
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/captions") as websocket:
            start_response = client.post(f"/api/lessons/{lesson['lesson_id']}/start")
            inject_response = client.post(
                f"/api/lessons/{lesson['lesson_id']}/debug/inject-rtms-audio",
                json={"chunks": 6, "sample_rate": 16000, "channels": 1, "chunk_size": 320},
            )
            payload = websocket.receive_json()
            while not payload["is_final"]:
                payload = websocket.receive_json()
        status = client.get(f"/api/lessons/{lesson['lesson_id']}/rtms").json()

    assert start_response.status_code == 200
    assert inject_response.status_code == 200
    assert payload["audio_source"] == "zoom_rtms"
    assert payload["audio"]["sample_rate"] == 16000
    assert set(payload["translations"]) == {"kk", "uz", "zh-Hans"}
    assert status["audio_chunks_received"] >= 6
    assert status["pipeline_chunks_processed"] >= 4
    assert status["stt_events_generated"] >= 2
    assert status["captions_sent"] >= 1


def test_inject_rtms_audio_endpoint_only_development(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'stage4a-prod.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("RTMS_EXPERIMENTAL_ENABLED", "true")
    app = create_app()

    with TestClient(app) as client:
        response = client.post("/api/lessons/lesson_missing/debug/inject-rtms-audio", json={"chunks": 1})

    assert response.status_code == 403


class _FakeZoomClient:
    async def create_meeting(self, title: str):
        from app.zoom.models import ZoomMeeting

        return ZoomMeeting(
            meeting_id="123456789",
            meeting_uuid="uuid_stage4a",
            join_url="https://zoom.us/j/123456789",
            start_url="https://zoom.us/s/123456789",
            topic=title,
            created_at="2026-05-08T10:00:00Z",
        )
