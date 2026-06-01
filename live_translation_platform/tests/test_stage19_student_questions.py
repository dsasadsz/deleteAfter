import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app


def test_text_question_in_ru_stores_original_and_translation_same(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "text-ru.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/questions/text",
            json={"student_name": "Student", "source_language": "ru", "text": "Можно повторить про классы?"},
        )
        listed = client.get(f"/api/lessons/{lesson['lesson_id']}/questions")

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["input_type"] == "text"
    assert payload["original_text"] == "Можно повторить про классы?"
    assert payload["translated_text_ru"] == "Можно повторить про классы?"
    assert payload["status"] == "new"
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == payload["id"]


def test_text_question_kk_uses_translator_to_ru(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "text-kk.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/questions/text",
            json={"student_id": "student-1", "student_name": "Aruzhan", "source_language": "kk", "text": "Сұрақ бар"},
        )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["student_id"] == "student-1"
    assert payload["student_name"] == "Aruzhan"
    assert payload["source_language"] == "kk"
    assert payload["translated_text_ru"] == "[ru mock] Сұрақ бар"
    assert payload["translation_provider"] == "mock"


def test_question_created_event_broadcasts_to_question_ws(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "broadcast.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/questions") as websocket:
            response = client.post(
                f"/api/lessons/{lesson['lesson_id']}/questions/text",
                json={"student_name": "Student", "source_language": "ru", "text": "Что такое interface?"},
            )
            event = websocket.receive_json()

    assert response.status_code == 201, response.text
    assert event["event"] == "question_created"
    assert event["lesson_id"] == lesson["lesson_id"]
    assert event["question"]["translated_text_ru"] == "Что такое interface?"


def test_mark_answered_and_dismiss_update_status(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "moderation.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        first = client.post(
            f"/api/lessons/{lesson['lesson_id']}/questions/text",
            json={"student_name": "Student", "source_language": "ru", "text": "Первый вопрос"},
        ).json()
        second = client.post(
            f"/api/lessons/{lesson['lesson_id']}/questions/text",
            json={"student_name": "Student", "source_language": "ru", "text": "Второй вопрос"},
        ).json()
        answered = client.post(f"/api/lessons/{lesson['lesson_id']}/questions/{first['id']}/answer")
        dismissed = client.post(f"/api/lessons/{lesson['lesson_id']}/questions/{second['id']}/dismiss")

    assert answered.status_code == 200, answered.text
    assert answered.json()["status"] == "answered"
    assert answered.json()["answered_at"] is not None
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["status"] == "dismissed"
    assert dismissed.json()["dismissed_at"] is not None


def test_voice_question_ws_creates_question_with_mock_stt(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENT_QUESTION_STT_PROVIDER", "mock")
    app = _app(tmp_path, monkeypatch, "voice.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/questions") as questions_ws:
            with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/student-question-audio") as audio_ws:
                audio_ws.send_text(
                    json.dumps(
                        {
                            "event": "question_audio_metadata",
                            "student_id": "student-voice",
                            "student_name": "Voice Student",
                            "source_language": "kk",
                            "sample_rate": 16000,
                            "channels": 1,
                            "format": "pcm_s16le",
                            "chunk_ms": 100,
                        }
                    )
                )
                audio_ws.send_bytes("дауыс сұрағы".encode())
                audio_ws.send_text(json.dumps({"event": "finish_question"}))
                audio_result = audio_ws.receive_json()
            event = questions_ws.receive_json()
        listed = client.get(f"/api/lessons/{lesson['lesson_id']}/questions")

    assert audio_result["event"] == "question_created"
    assert audio_result["question"]["input_type"] == "voice"
    assert audio_result["question"]["recognized_text"] == "дауыс сұрағы"
    assert event["event"] == "question_created"
    assert event["question"]["recognized_text"] == "дауыс сұрағы"
    assert listed.json()[0]["input_type"] == "voice"


def test_voice_question_ws_enforces_max_queue_size(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDENT_QUESTION_STT_PROVIDER", "mock")
    monkeypatch.setenv("STUDENT_QUESTION_MAX_QUEUE_SIZE", "1")
    app = _app(tmp_path, monkeypatch, "voice-queue.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/student-question-audio") as audio_ws:
            audio_ws.send_text(json.dumps({"event": "question_audio_metadata", "source_language": "ru"}))
            audio_ws.send_bytes(b"first")
            audio_ws.send_bytes(b"second")
            result = audio_ws.receive_json()

    assert result["event"] == "question_error"
    assert "queue" in result["error"].lower()


def test_question_ws_requires_question_read_scope_when_auth_enabled(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "question-auth.db", websocket_auth_enabled=True)

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with pytest.raises(WebSocketDisconnect) as missing:
            with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/questions"):
                pass
        token = _token("teacher-1", "teacher", lesson["lesson_id"], ["question:read"])
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/questions?token={token}") as websocket:
            websocket.send_text("ping")

    assert missing.value.code == 4401


def test_question_http_routes_require_scopes_when_auth_enabled(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "question-http-auth.db", websocket_auth_enabled=True)

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        missing = client.post(
            f"/api/lessons/{lesson['lesson_id']}/questions/text",
            json={"student_name": "Student", "source_language": "ru", "text": "Auth question"},
        )
        read_only_token = _token("student-1", "student", lesson["lesson_id"], ["question:read"])
        forbidden = client.post(
            f"/api/lessons/{lesson['lesson_id']}/questions/text?token={read_only_token}",
            json={"student_name": "Student", "source_language": "ru", "text": "Auth question"},
        )
        write_token = _token("student-1", "student", lesson["lesson_id"], ["question:write"])
        created = client.post(
            f"/api/lessons/{lesson['lesson_id']}/questions/text?token={write_token}",
            json={"student_name": "Student", "source_language": "ru", "text": "Auth question"},
        )

    assert missing.status_code == 401
    assert forbidden.status_code == 403
    assert created.status_code == 201, created.text


def test_answer_with_wrong_lesson_does_not_mutate_question(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "wrong-lesson-moderation.db")

    with TestClient(app) as client:
        lesson_a = _create_lesson(client)
        lesson_b = _create_lesson(client)
        question = client.post(
            f"/api/lessons/{lesson_a['lesson_id']}/questions/text",
            json={"student_name": "Student", "source_language": "ru", "text": "Do not mutate"},
        ).json()
        wrong_answer = client.post(f"/api/lessons/{lesson_b['lesson_id']}/questions/{question['id']}/answer")
        listed = client.get(f"/api/lessons/{lesson_a['lesson_id']}/questions")

    assert wrong_answer.status_code == 404
    assert listed.json()[0]["status"] == "new"


def test_student_and_teacher_pages_render_question_panels(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "question-ui.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        student = client.get(f"/student/{lesson['lesson_id']}")
        teacher = client.get(f"/teacher/{lesson['lesson_id']}")

    assert student.status_code == 200
    assert "Ask teacher" in student.text
    assert "student_questions.js" in student.text
    assert "STUDENT_QUESTION_AUDIO_ENABLED" not in student.text
    assert teacher.status_code == 200
    assert "Student Questions" in teacher.text
    assert "teacher_questions.js" in teacher.text


def test_student_question_text_js_forwards_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "question-js-token.db", websocket_auth_enabled=True)

    with TestClient(app) as client:
        response = client.get("/static/student_questions.js")

    assert response.status_code == 200
    assert "questions/text?token=" in response.text


def _app(tmp_path, monkeypatch, db_name: str, websocket_auth_enabled: bool = False):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("STT_PROVIDER", "mock")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage19-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "true" if websocket_auth_enabled else "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "false" if websocket_auth_enabled else "true")
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post(
        "/api/lessons",
        json={"title": "Stage 19", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock", "target_languages": ["kk"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _token(sub: str, role: str, lesson_id: str, scopes: list[str]) -> str:
    from app.security.tokens import create_access_token

    return create_access_token(
        {"sub": sub, "role": role, "lesson_id": lesson_id, "external_lesson_id": "ext-stage19", "scopes": scopes},
        ttl_seconds=3600,
    )
