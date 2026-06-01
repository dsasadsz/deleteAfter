import asyncio
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.audio.browser_mic_audio_source import BrowserMicAudioSource
from app.main import create_app
from app.realtime.audio_pipeline import AudioPipeline
from app.realtime.browser_audio_manager import BrowserAudioManager
from app.schemas.browser_audio import BrowserAudioStatus, BrowserAudioTuning
from app.stt.base import STTEvent
from app.translation.mock_translator import MockTranslator
from app.zoom.models import ZoomMeeting


class DummyWebSocket:
    def __init__(self) -> None:
        self.close_code = None

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


@pytest.mark.asyncio
async def test_browser_audio_manager_accepts_metadata_message(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-meta.db').as_posix()}")
    app = create_app()
    manager = app.state.browser_audio_manager

    await manager.handle_text(
        "lesson_1",
        json.dumps(
            {
                "event": "audio_metadata",
                "sample_rate": 16000,
                "channels": 1,
                "format": "pcm_s16le",
                "chunk_ms": 100,
                "source": "browser_mic",
            }
        ),
    )

    status = manager.get_status("lesson_1")
    assert status.status == BrowserAudioStatus.WAITING_FOR_TEACHER
    assert status.config.expected_sample_rate == 16000
    assert manager.metadata_for_lesson("lesson_1")["format"] == "pcm_s16le"


@pytest.mark.asyncio
async def test_browser_audio_manager_accepts_binary_audio_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-chunk.db').as_posix()}")
    app = create_app()
    manager = app.state.browser_audio_manager
    client_sent_at = datetime.utcnow() - timedelta(milliseconds=25)

    await manager.handle_text("lesson_1", json.dumps({"event": "audio_chunk", "client_sent_at": client_sent_at.isoformat()}))
    accepted = await manager.handle_binary("lesson_1", b"\x01\x02" * 160)
    event = await manager.get_audio_queue("lesson_1").get()

    assert accepted is True
    assert event["kind"] == "audio"
    assert event["data"] == b"\x01\x02" * 160
    assert event["client_sent_at"] == client_sent_at
    assert event["server_received_at"] >= client_sent_at
    assert manager.get_status("lesson_1").chunks_received == 1
    assert manager.get_status("lesson_1").bytes_received == 320


@pytest.mark.asyncio
async def test_stale_browser_audio_disconnect_does_not_override_active_connection():
    manager = BrowserAudioManager(allow_duplicate_teacher=True)
    first = DummyWebSocket()
    second = DummyWebSocket()

    assert await manager.connect("lesson_1", first) is True
    assert await manager.connect("lesson_1", second) is True
    await manager.disconnect("lesson_1", first)

    assert manager.connections["lesson_1"] is second
    assert manager.get_status("lesson_1").status == BrowserAudioStatus.CONNECTED


@pytest.mark.asyncio
async def test_browser_audio_status_hides_metadata_after_disconnect():
    manager = BrowserAudioManager()
    websocket = DummyWebSocket()

    assert await manager.connect("lesson_1", websocket) is True
    await manager.handle_text("lesson_1", json.dumps({"event": "audio_metadata", "sample_rate": 16000, "channels": 1, "format": "pcm_s16le"}))
    await manager.disconnect("lesson_1", websocket)
    status = manager.get_status("lesson_1")

    assert status.ws_connected is False
    assert status.metadata_received is False
    assert status.active_connection_id is None


@pytest.mark.asyncio
async def test_browser_audio_queue_overflow_drop_oldest(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-overflow.db').as_posix()}")
    app = create_app()
    manager = BrowserAudioManager(
        session_factory=app.state.database.session_factory,
        hub=app.state.caption_hub,
        debug_repo=app.state.debug_repo,
        queue_max_size=1,
        drop_policy="drop_oldest",
    )

    await manager.handle_binary("lesson_1", b"first")
    await manager.handle_binary("lesson_1", b"second")
    status = manager.get_status("lesson_1")
    event = await manager.get_audio_queue("lesson_1").get()

    assert event["data"] == b"second"
    assert status.chunks_received == 2
    assert status.chunks_dropped == 1
    assert status.queue_size == 1


@pytest.mark.asyncio
async def test_browser_mic_audio_source_yields_audio_chunk(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-source.db').as_posix()}")
    app = create_app()
    manager = app.state.browser_audio_manager
    source = BrowserMicAudioSource("lesson_source", manager)
    client_sent_at = datetime.utcnow() - timedelta(milliseconds=40)

    await manager.handle_text(
        "lesson_source",
        json.dumps({"event": "audio_metadata", "sample_rate": 16000, "channels": 1, "format": "pcm_s16le"}),
    )
    await manager.handle_text("lesson_source", json.dumps({"event": "audio_chunk", "client_sent_at": client_sent_at.isoformat()}))
    await manager.handle_binary("lesson_source", b"pcm")
    chunk = await anext(source.chunks())

    assert chunk.data == b"pcm"
    assert chunk.lesson_id == "lesson_source"
    assert chunk.source == "browser_ws"
    assert chunk.sample_rate == 16000
    assert chunk.channels == 1
    assert chunk.format == "pcm_s16le"
    assert chunk.client_sent_at == client_sent_at
    assert chunk.server_received_at is not None
    assert source.chunks_yielded == 1


@pytest.mark.asyncio
async def test_browser_audio_force_commit_yields_control_chunk(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-force-commit.db').as_posix()}")
    app = create_app()
    manager = app.state.browser_audio_manager
    source = BrowserMicAudioSource("lesson_force", manager)

    await manager.handle_text("lesson_force", json.dumps({"event": "force_commit"}))
    chunk = await asyncio.wait_for(anext(source.chunks()), timeout=1)

    assert chunk.data == b""
    assert chunk.source == "browser_ws"
    assert chunk.metadata["control"] == "stt_commit"
    assert chunk.metadata["reason"] == "teacher_force_commit"


@pytest.mark.asyncio
async def test_browser_audio_manager_auto_commits_after_silence(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-silence-commit.db').as_posix()}")
    app = create_app()
    manager = BrowserAudioManager(
        session_factory=app.state.database.session_factory,
        hub=app.state.caption_hub,
        debug_repo=app.state.debug_repo,
        commit_strategy="manual",
        manual_commit_after_silence_ms=200,
        chunk_ms=100,
        silence_rms_threshold=0.01,
    )

    await manager.handle_binary("lesson_silence", b"\xff\x7f" * 1600)
    await manager.handle_binary("lesson_silence", b"\x00\x00" * 1600)
    await manager.handle_binary("lesson_silence", b"\x00\x00" * 1600)

    queued = []
    queue = manager.get_audio_queue("lesson_silence")
    while not queue.empty():
        queued.append(await queue.get())

    assert [event["kind"] for event in queued][-1] == "stt_commit"
    assert queued[-1]["metadata"]["reason"] == "silence_timeout"


@pytest.mark.asyncio
async def test_browser_audio_tuning_is_per_lesson(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-tuning.db').as_posix()}")
    app = create_app()
    manager = app.state.browser_audio_manager

    manager.update_tuning(
        "lesson_a",
        BrowserAudioTuning(
            chunk_ms=80,
            commit_strategy="manual",
            silence_commit_ms=300,
            partials_enabled=False,
            force_commit_enabled=True,
            max_segment_duration_ms=2500,
            rms_threshold=0.02,
            updated_by="teacher_a",
        ),
    )

    lesson_a = manager.tuning_for_lesson("lesson_a")
    lesson_b = manager.tuning_for_lesson("lesson_b")

    assert lesson_a.chunk_ms == 80
    assert lesson_a.commit_strategy == "manual"
    assert lesson_a.silence_commit_ms == 300
    assert lesson_a.partials_enabled is False
    assert lesson_a.max_segment_duration_ms == 2500
    assert lesson_a.rms_threshold == 0.02
    assert lesson_a.updated_by == "teacher_a"
    assert lesson_b.chunk_ms == app.state.settings.browser_audio_chunk_ms
    assert lesson_b.commit_strategy == app.state.settings.elevenlabs_stt_commit_strategy
    assert lesson_b.max_segment_duration_ms == app.state.settings.browser_audio_max_segment_duration_ms


def test_browser_audio_tuning_endpoints_are_per_lesson(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-tuning-endpoints.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson_a = client.post("/api/lessons", json={"title": "A", "mode": "mock"}).json()
        lesson_b = client.post("/api/lessons", json={"title": "B", "mode": "mock"}).json()
        update = client.put(
            f"/api/lessons/{lesson_a['lesson_id']}/browser-audio/tuning",
            json={
                "chunk_ms": 70,
                "commit_strategy": "manual",
                "silence_commit_ms": 400,
                "partials_enabled": False,
                "force_commit_enabled": True,
                "max_segment_duration_ms": 3000,
                "rms_threshold": 0.015,
                "updated_by": "teacher",
            },
        )
        lesson_a_tuning = client.get(f"/api/lessons/{lesson_a['lesson_id']}/browser-audio/tuning")
        lesson_b_tuning = client.get(f"/api/lessons/{lesson_b['lesson_id']}/browser-audio/tuning")

    assert update.status_code == 200
    assert lesson_a_tuning.json()["chunk_ms"] == 70
    assert lesson_a_tuning.json()["commit_strategy"] == "manual"
    assert lesson_a_tuning.json()["max_segment_duration_ms"] == 3000
    assert lesson_b_tuning.json()["chunk_ms"] == 100
    assert lesson_b_tuning.json()["commit_strategy"] == "vad"


@pytest.mark.asyncio
async def test_browser_audio_max_segment_duration_triggers_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-max-segment.db').as_posix()}")
    app = create_app()
    manager = BrowserAudioManager(
        session_factory=app.state.database.session_factory,
        hub=app.state.caption_hub,
        debug_repo=app.state.debug_repo,
        commit_strategy="manual",
        chunk_ms=100,
        manual_commit_after_silence_ms=0,
        max_segment_duration_ms=300,
        periodic_commit_enabled=True,
        silence_rms_threshold=0.001,
    )

    await manager.handle_binary("lesson_segment", b"\xff\x7f" * 1600)
    await manager.handle_binary("lesson_segment", b"\xff\x7f" * 1600)
    await manager.handle_binary("lesson_segment", b"\xff\x7f" * 1600)

    queued = []
    queue = manager.get_audio_queue("lesson_segment")
    while not queue.empty():
        queued.append(await queue.get())

    assert [event["kind"] for event in queued][-1] == "stt_commit"
    assert queued[-1]["metadata"]["reason"] == "max_segment_duration"
    assert queued[-1]["metadata"]["segment_duration_ms"] == 300


@pytest.mark.asyncio
async def test_audio_pipeline_force_commit_calls_stt_provider_commit():
    commits = []
    manager = BrowserAudioManager(force_commit_enabled=True)
    source = BrowserMicAudioSource("lesson_pipeline_commit", manager)
    provider = CommitAwareSTTProvider(commits)
    pipeline = AudioPipeline(
        lesson_id="lesson_pipeline_commit",
        meeting_id="123",
        source=source,
        stt=provider,
        translator=MockTranslator(),
        target_languages=["kk"],
        translate_partials=False,
        publish=lambda payload: _append([], payload),
        save_caption=lambda payload: None,
        save_metric=lambda payload: None,
        publish_debug=lambda payload: None,
    )

    await pipeline.start()
    await manager.request_commit("lesson_pipeline_commit", reason="teacher_force_commit")
    await asyncio.sleep(0.05)
    await pipeline.stop()

    assert commits == ["teacher_force_commit"]


def test_browser_ws_lesson_with_mock_pipeline_emits_caption_with_latency(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-pipeline.db').as_posix()}")
    monkeypatch.setenv("MOCK_STT_AUDIO_DRIVEN", "false")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        lesson = client.post(
            "/api/lessons",
            json={
                "title": "Browser WS",
                "mode": "zoom",
                "audio_source": "browser_ws",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk"],
            },
        ).json()
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/captions") as captions:
            start = client.post(f"/api/lessons/{lesson['lesson_id']}/start")
            with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/audio-ingest") as audio:
                audio.send_text(
                    json.dumps(
                        {
                            "event": "audio_metadata",
                            "sample_rate": 16000,
                            "channels": 1,
                            "format": "pcm_s16le",
                            "chunk_ms": 100,
                        }
                    )
                )
                audio.send_text(json.dumps({"event": "audio_chunk", "client_sent_at": (datetime.utcnow() - timedelta(milliseconds=15)).isoformat()}))
                audio.send_bytes("привет из браузера".encode())
                payload = captions.receive_json()
                while not payload["is_final"]:
                    payload = captions.receive_json()

    assert start.status_code == 200
    assert payload["audio_source"] == "browser_ws"
    assert payload["timestamps"]["client_audio_sent_at"]
    assert payload["timestamps"]["mic_client_capture_at"]
    assert payload["timestamps"]["audio_ws_sent_at"]
    assert payload["timestamps"]["audio_server_received_at"]
    assert payload["timestamps"]["audio_pipeline_received_at"]
    assert payload["timestamps"]["stt_final_at"]
    assert "client_caption_received_at" in payload["timestamps"]
    assert payload["latency_ms"]["ingest_latency_ms"] >= 0
    assert payload["latency_ms"]["final_latency_ms"] >= 0
    assert payload["latency_ms"]["stt_latency_ms"] >= 0
    assert payload["latency_ms"]["translation_latency_ms"] >= 0
    assert payload["latency_ms"]["total_latency_ms"] >= payload["latency_ms"]["translation_latency_ms"]
    assert payload["latency_ms"]["total_server_latency_ms"] >= payload["latency_ms"]["stt_latency_ms"]
    assert payload["latency_ms"]["estimated_end_to_end_latency_ms"] >= payload["latency_ms"]["total_server_latency_ms"]
    assert "pipeline_queue_size" in payload["audio"]
    assert "dropped_chunks" in payload["audio"]
    assert "commit_reason" in payload["audio"]
    assert "segment_duration_ms" in payload["audio"]


def test_audio_ingest_websocket_accepts_connection_and_pushes_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-ws.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        with client.websocket_connect("/ws/lessons/lesson_ws/audio-ingest") as websocket:
            websocket.send_text(json.dumps({"event": "audio_metadata", "sample_rate": 16000, "channels": 1, "format": "pcm_s16le"}))
            websocket.send_bytes(b"abc")
            status = client.get("/api/lessons/lesson_ws/browser-audio")

    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == BrowserAudioStatus.RECEIVING_AUDIO
    assert payload["chunks_received"] == 1
    assert payload["bytes_received"] == 3
    assert payload["ws_connected"] is True
    assert payload["has_active_connection"] is True
    assert payload["active_connection_id"]
    assert payload["latest_connection_id"] == payload["active_connection_id"]
    assert payload["metadata_received"] is True
    assert payload["binary_frames_received"] == 1
    assert payload["last_binary_frame_at"]


def test_audio_ingest_websocket_accepts_browser_metadata_timestamp_and_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'browser-ws-metadata.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Browser metadata", "mode": "mock"}).json()
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/audio-ingest") as websocket:
            connected = client.get(f"/api/lessons/{lesson['lesson_id']}/browser-audio").json()
            assert connected["ws_connected"] is True
            assert connected["status"] == BrowserAudioStatus.CONNECTED

            websocket.send_text(
                json.dumps(
                    {
                        "event": "audio_metadata",
                        "sample_rate": 16000,
                        "channels": 1,
                        "format": "pcm_s16le",
                        "chunk_ms": 100,
                        "client_started_at": datetime.utcnow().isoformat(),
                    }
                )
            )
            websocket.send_bytes(b"\x01\x02" * 160)
            status = client.get(f"/api/lessons/{lesson['lesson_id']}/browser-audio").json()

    assert status["status"] == BrowserAudioStatus.RECEIVING_AUDIO
    assert status["ws_connected"] is True
    assert status["chunks_received"] == 1
    assert status["bytes_received"] == 320
    assert status["metadata_received"] is True


def test_teacher_page_renders_browser_mic_panel(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'teacher-page.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Teacher", "mode": "mock"}).json()
        response = client.get(f"/teacher/{lesson['lesson_id']}")
        script = client.get("/static/teacher_mic.js")

    assert response.status_code == 200
    assert "Teacher Microphone Audio" in response.text
    assert "teacher_mic.js" in response.text
    assert "worklet_messages_received" in response.text
    assert "float_frames_received" in response.text
    assert "pcm_frames_sent" in response.text
    assert "binary_ws_frames_sent" in response.text
    assert "ws_ready_state" in response.text
    assert "audio_context_state" in response.text
    assert "input_sample_rate" in response.text
    assert "capture_backend" in response.text
    assert "last_frontend_error" in response.text
    assert "fallback_reason" in response.text
    assert "frontend_initialized" in response.text
    assert "start_button_bound" in response.text
    assert "start_clicked_count" in response.text
    assert "lesson_id_from_dom" in response.text
    assert "audio_ingest_url" in response.text
    assert "STT chunks sent" in response.text
    assert "STT last error" in response.text
    assert "sttProviderLastError" in response.text
    assert "pollLessonDiagnostics" in script.text
    assert "teacher_mic.js?v=" in response.text


def test_teacher_page_uses_safe_defaults_for_legacy_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'teacher-legacy-settings.db').as_posix()}")
    app = create_app()
    app.state.settings = LegacyTeacherSettings()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Teacher legacy", "mode": "mock"}).json()
        response = client.get(f"/teacher/{lesson['lesson_id']}")

    assert response.status_code == 200
    assert "Teacher Microphone Audio" in response.text
    assert "Force STT commit" in response.text


def test_teacher_page_embeds_audio_token_when_websocket_auth_required(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'teacher-token-page.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "true")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "teacher-page-secret")
    app = create_app()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Teacher token", "mode": "mock"}).json()
        response = client.get(f"/teacher/{lesson['lesson_id']}")

    assert response.status_code == 200
    assert 'data-audio-token=""' not in response.text
    assert "INTEGRATION_API_KEYS" not in response.text


def test_student_page_renders_audio_source_badge(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'student-page.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Student", "mode": "mock"}).json()
        response = client.get(f"/student/{lesson['lesson_id']}")

    assert response.status_code == 200
    assert "audio_source" in response.text
    assert "captionLatencyBreakdown" in response.text


def test_real_test_page_supports_audio_source_selector(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'real-audio-source.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/real-test")

    assert response.status_code == 200
    assert "realAudioSource" in response.text
    assert "Browser Mic WebSocket" in response.text


def test_create_zoom_lesson_accepts_browser_ws_audio_source(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'lesson-source.db').as_posix()}")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        response = client.post(
            "/api/lessons",
            json={"title": "Browser source", "mode": "zoom", "audio_source": "browser_ws"},
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["mode"] == "zoom"
    assert payload["audio_source"] == "browser_ws"
    assert payload["browser_audio_status"] == BrowserAudioStatus.WAITING_FOR_TEACHER


class FakeZoomAPIClient:
    async def create_meeting(self, title: str) -> ZoomMeeting:
        return ZoomMeeting(
            meeting_id="123456789",
            meeting_uuid="uuid_browser_ws",
            join_url="https://zoom.us/j/123456789?pwd=pass123",
            start_url="https://zoom.us/s/123456789?zak=secret",
            topic=title,
            created_at="2026-05-12T10:00:00Z",
            password="pass123",
        )


class LegacyTeacherSettings:
    rtms_ui_enabled = False
    rtms_experimental_enabled = False
    browser_audio_chunk_ms = 100
    browser_audio_max_segment_duration_ms = 5000
    elevenlabs_stt_commit_strategy = "vad"


async def _append(items, item):
    items.append(item)


class CommitAwareSTTProvider:
    name = "commit_aware"

    def __init__(self, commits):
        self.commits = commits
        self._queue = asyncio.Queue()

    async def connect(self):
        return None

    async def send_audio(self, audio_chunk, metadata=None):
        await self._queue.put(
            STTEvent(
                text="ignored",
                is_partial=False,
                is_final=True,
                language="ru-RU",
                confidence=None,
                provider=self.name,
                timestamp=datetime.utcnow(),
                raw={"audio_source": (metadata or {}).get("source", "browser_ws")},
                audio_received_at=(metadata or {}).get("audio_received_at"),
            )
        )

    async def commit(self, reason: str | None = None):
        self.commits.append(reason)

    async def events(self):
        while True:
            yield await self._queue.get()

    async def close(self):
        return None
