import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app


def test_voice_question_no_final_timeout_persists_error_and_notifies_teacher(tmp_path, monkeypatch):
    _patch_stt(monkeypatch, NoFinalSTT())
    monkeypatch.setenv("STUDENT_QUESTION_FINAL_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setenv("STUDENT_QUESTION_STT_TOTAL_TIMEOUT_SECONDS", "1")
    app = _app(tmp_path, monkeypatch, "voice-final-timeout.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/questions") as questions_ws:
            with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/student-question-audio") as audio_ws:
                _send_metadata(audio_ws, student_name="Timeout Student")
                audio_ws.send_bytes(b"audio")
                audio_ws.send_text(json.dumps({"event": "finish_question"}))
                student_event = audio_ws.receive_json()
                with pytest.raises(WebSocketDisconnect):
                    audio_ws.receive_json()
            teacher_event = questions_ws.receive_json()
        listed = client.get(f"/api/lessons/{lesson['lesson_id']}/questions")

    assert student_event["event"] == "question_error"
    assert student_event["code"] == "stt_final_timeout"
    assert "final transcript" in student_event["error"].lower()
    assert student_event["question"]["status"] == "error"
    assert student_event["question"]["student_name"] == "Timeout Student"
    assert teacher_event["event"] == "question_error"
    assert teacher_event["code"] == "stt_final_timeout"
    assert teacher_event["question"]["id"] == student_event["question"]["id"]
    stored = listed.json()[0]
    assert stored["status"] == "error"
    metadata = json.loads(stored["metadata_json"])
    assert metadata["error"]["code"] == "stt_final_timeout"
    assert metadata["stt_provider"] == "fake_no_final"


def test_voice_question_max_audio_bytes_exceeded_returns_error_and_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENT_QUESTION_MAX_AUDIO_BYTES", "4")
    app = _app(tmp_path, monkeypatch, "voice-max-bytes.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/student-question-audio") as audio_ws:
            _send_metadata(audio_ws)
            audio_ws.send_bytes(b"12345")
            event = audio_ws.receive_json()
            with pytest.raises(WebSocketDisconnect):
                audio_ws.receive_json()
        listed = client.get(f"/api/lessons/{lesson['lesson_id']}/questions")

    assert event["event"] == "question_error"
    assert event["code"] == "audio_too_large"
    assert "bytes" in event["error"].lower()
    assert listed.json()[0]["status"] == "error"


def test_voice_question_max_duration_exceeded_returns_error_and_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENT_QUESTION_MAX_DURATION_SECONDS", "0")
    app = _app(tmp_path, monkeypatch, "voice-max-duration.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/student-question-audio") as audio_ws:
            _send_metadata(audio_ws, chunk_ms=100)
            audio_ws.send_bytes(b"audio")
            event = audio_ws.receive_json()
            with pytest.raises(WebSocketDisconnect):
                audio_ws.receive_json()

    assert event["event"] == "question_error"
    assert event["code"] == "audio_too_long"
    assert "duration" in event["error"].lower()
    assert event["question"]["audio_duration_ms"] == 100


def test_voice_question_provider_disconnect_is_saved_in_metadata(tmp_path, monkeypatch):
    _patch_stt(monkeypatch, DisconnectingSTT())
    app = _app(tmp_path, monkeypatch, "voice-provider-disconnect.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/student-question-audio") as audio_ws:
            _send_metadata(audio_ws)
            audio_ws.send_bytes(b"audio")
            audio_ws.send_text(json.dumps({"event": "finish_question"}))
            event = audio_ws.receive_json()
        listed = client.get(f"/api/lessons/{lesson['lesson_id']}/questions")

    assert event["event"] == "question_error"
    assert event["code"] == "provider_error"
    stored = listed.json()[0]
    assert stored["error"] == "Student question STT provider disconnected."
    metadata = json.loads(stored["metadata_json"])
    assert metadata["error"]["code"] == "provider_error"
    assert "disconnected" in metadata["error"]["detail"].lower()


def test_voice_question_streams_audio_chunks_to_stt_provider(tmp_path, monkeypatch):
    provider = ChunkCountingSTT()
    _patch_stt(monkeypatch, provider)
    app = _app(tmp_path, monkeypatch, "voice-stream-chunks.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/student-question-audio") as audio_ws:
            _send_metadata(audio_ws)
            audio_ws.send_bytes(b"first")
            audio_ws.send_bytes(b"second")
            audio_ws.send_text(json.dumps({"event": "finish_question"}))
            event = audio_ws.receive_json()

    assert event["event"] == "question_created"
    assert provider.sent_chunks == [b"first", b"second"]
    assert provider.commit_reason == "student_question_finish"


def test_voice_question_final_timeout_metadata_includes_provider_last_error(tmp_path, monkeypatch):
    _patch_stt(monkeypatch, EndingSTT(last_error="commit_throttled"))
    monkeypatch.setenv("STUDENT_QUESTION_FINAL_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("STUDENT_QUESTION_STT_TOTAL_TIMEOUT_SECONDS", "2")
    app = _app(tmp_path, monkeypatch, "voice-final-provider-error.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/student-question-audio") as audio_ws:
            _send_metadata(audio_ws)
            audio_ws.send_bytes(b"audio")
            audio_ws.send_text(json.dumps({"event": "finish_question"}))
            event = audio_ws.receive_json()
        listed = client.get(f"/api/lessons/{lesson['lesson_id']}/questions")

    assert event["event"] == "question_error"
    stored = listed.json()[0]
    metadata = json.loads(stored["metadata_json"])
    assert metadata["provider_last_error"] == "commit_throttled"
    assert "commit_throttled" in metadata["error"]["detail"]


def test_student_question_stt_kwargs_use_per_question_language(monkeypatch):
    from app.config import Settings
    from app.questions.audio_handler import StudentQuestionAudioHandler

    monkeypatch.setenv("AZURE_SPEECH_KEY", "key")
    monkeypatch.setenv("AZURE_SPEECH_REGION", "region")
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-key")
    settings = Settings(_env_file=None)
    handler = StudentQuestionAudioHandler(None, settings)

    assert handler._stt_kwargs("azure", {"source_language": "kk"})["language"] == "kk-KZ"
    assert handler._stt_kwargs("azure", {"source_language": "ru"})["language"] == "ru-RU"
    assert handler._stt_kwargs("cartesia", {"source_language": "kk"})["language"] == "kk"
    assert handler._stt_kwargs("cartesia", {"source_language": "ru"})["language"] == "ru"


def test_student_question_ui_handles_error_event_and_resets_recording_state(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "voice-ui-js.db")

    with TestClient(app) as client:
        response = client.get("/static/student_questions.js")
        student_page = client.get(f"/student/{_create_lesson(client)['lesson_id']}")

    assert response.status_code == 200
    assert "payload.event === \"question_error\"" in response.text
    assert "payload.code" in response.text
    assert "cleanupStudentVoice(false)" in response.text
    assert "questionDisplayText" in response.text
    assert "metadata_json" in response.text
    assert "data-max-audio-bytes" in student_page.text
    assert "data-max-duration-seconds" in student_page.text


def test_stage23b_env_example_documents_voice_question_limits():
    env = open(".env.example", encoding="utf-8").read()

    assert "STUDENT_QUESTION_MAX_AUDIO_BYTES=1048576" in env
    assert "STUDENT_QUESTION_FINAL_TIMEOUT_SECONDS=10" in env
    assert "STUDENT_QUESTION_STT_CONNECT_TIMEOUT_SECONDS=5" in env
    assert "STUDENT_QUESTION_STT_TOTAL_TIMEOUT_SECONDS=25" in env


def _app(tmp_path, monkeypatch, db_name: str):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("STT_PROVIDER", "mock")
    monkeypatch.setenv("STUDENT_QUESTION_STT_PROVIDER", "mock")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage23b-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post(
        "/api/lessons",
        json={"title": "Stage 23B", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock", "target_languages": ["kk"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _send_metadata(audio_ws, *, student_name: str = "Voice Student", chunk_ms: int = 100) -> None:
    audio_ws.send_text(
        json.dumps(
            {
                "event": "question_audio_metadata",
                "student_id": "student-voice",
                "student_name": student_name,
                "source_language": "kk",
                "sample_rate": 16000,
                "channels": 1,
                "format": "pcm_s16le",
                "chunk_ms": chunk_ms,
            }
        )
    )


def _patch_stt(monkeypatch, provider) -> None:
    monkeypatch.setattr("app.questions.audio_handler.create_stt_provider", lambda _name, **_kwargs: provider)


class NoFinalSTT:
    name = "fake_no_final"
    supports_commit = True

    async def connect(self) -> None:
        return None

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        return None

    async def commit(self, reason: str | None = None) -> None:
        return None

    async def events(self):
        while True:
            await asyncio.sleep(3600)
            yield None

    async def close(self) -> None:
        return None


class DisconnectingSTT:
    name = "fake_disconnect"

    async def connect(self) -> None:
        raise RuntimeError("provider disconnected during connect")

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        return None

    async def events(self):
        if False:
            yield None

    async def close(self) -> None:
        return None


class ChunkCountingSTT:
    name = "chunk_counter"
    supports_commit = True

    def __init__(self) -> None:
        self.sent_chunks = []
        self.commit_reason = None

    async def connect(self) -> None:
        return None

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        self.sent_chunks.append(audio_chunk)

    async def commit(self, reason: str | None = None) -> None:
        self.commit_reason = reason

    async def events(self):
        from datetime import datetime
        from app.stt.base import STTEvent

        yield STTEvent(
            text="streamed question",
            is_partial=False,
            is_final=True,
            language="kk",
            confidence=0.9,
            provider=self.name,
            timestamp=datetime.utcnow(),
        )

    async def close(self) -> None:
        return None


class EndingSTT:
    name = "ending_stt"
    supports_commit = True

    def __init__(self, last_error: str | None = None) -> None:
        self.last_error = last_error

    async def connect(self) -> None:
        return None

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        return None

    async def commit(self, reason: str | None = None) -> None:
        return None

    async def events(self):
        if False:
            yield None

    async def close(self) -> None:
        return None
