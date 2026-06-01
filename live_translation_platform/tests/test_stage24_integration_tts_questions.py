import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.security.tokens import create_access_token, verify_access_token


def test_student_token_response_includes_tts_and_question_urls(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "student-token-urls.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/student-token",
            headers=_headers(),
            json={"external_student_id": "student-123", "display_name": "Aidos"},
        )

    payload = response.json()
    assert response.status_code == 200, response.text
    assert payload["lesson_id"] == lesson["lesson_id"]
    assert f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/status" in payload["tts_status_url"]
    assert f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/synthesize" in payload["tts_synthesize_url"]
    assert f"/ws/v1/lessons/{lesson['lesson_id']}/questions?token=" in payload["questions_websocket_url"]
    assert f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions/text" in payload["text_question_url"]
    assert f"/ws/v1/lessons/{lesson['lesson_id']}/student-question-audio?token=" in payload["voice_question_audio_websocket_url"]


def test_teacher_token_response_includes_question_moderation_urls(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "teacher-token-urls.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/teacher-token",
            headers=_headers(),
            json={"external_teacher_id": "teacher-123", "display_name": "Teacher"},
        )

    payload = response.json()
    assert response.status_code == 200, response.text
    assert payload["lesson_id"] == lesson["lesson_id"]
    assert f"/ws/v1/lessons/{lesson['lesson_id']}/questions?token=" in payload["questions_websocket_url"]
    assert f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions" in payload["questions_list_url"]
    assert payload["question_answer_url_template"].endswith(f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions/{{question_id}}/answer?token={payload['token']}")
    assert payload["question_dismiss_url_template"].endswith(f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions/{{question_id}}/dismiss?token={payload['token']}")


def test_student_and_teacher_default_token_scopes_include_tts_questions(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "default-scopes.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        student = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/student-token",
            headers=_headers(),
            json={"external_student_id": "student-123"},
        ).json()
        teacher = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/teacher-token",
            headers=_headers(),
            json={"external_teacher_id": "teacher-123"},
        ).json()

    assert set(verify_access_token(student["token"]).scopes) == {"captions:read", "zoom:embed", "tts:play", "question:write", "question:read"}
    assert set(verify_access_token(teacher["token"]).scopes) == {"audio:write", "diagnostics:read", "captions:read", "question:read", "question:moderate"}


def test_v1_tts_status_works_with_integration_key_and_tts_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-status.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        key_response = client.get(f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/status", headers=_headers())
        token = _token("student-1", "student", lesson["lesson_id"], ["tts:play"])
        token_response = client.get(f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/status?token={token}")

    assert key_response.status_code == 200, key_response.text
    assert token_response.status_code == 200, token_response.text
    assert token_response.json()["enabled"] is True
    assert "mock" in token_response.json()["providers"]


def test_v1_tts_synthesize_requires_tts_play_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-synthesize.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        play_token = _token("student-1", "student", lesson["lesson_id"], ["tts:play"])
        read_only_token = _token("student-1", "student", lesson["lesson_id"], ["captions:read"])
        ok = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/synthesize?token={play_token}",
            json={"text": "Salem", "language": "kk", "provider": "mock", "sequence": 123},
        )
        forbidden = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/synthesize?token={read_only_token}",
            json={"text": "Salem", "language": "kk", "provider": "mock"},
        )

    assert ok.status_code == 200, ok.text
    assert ok.content.startswith(b"RIFF")
    assert ok.headers["x-tts-provider"] == "mock"
    assert forbidden.status_code == 403


def test_v1_text_question_create_list_answer_and_dismiss_require_scopes(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "questions.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        write_token = _token("student-1", "student", lesson["lesson_id"], ["question:write"])
        read_token = _token("student-1", "student", lesson["lesson_id"], ["question:read"])
        moderate_token = _token("teacher-1", "teacher", lesson["lesson_id"], ["question:moderate"])
        created = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions/text?token={write_token}",
            json={"student_id": "student-1", "student_name": "Aidos", "source_language": "kk", "text": "Massiv degen ne?"},
        )
        forbidden_list = client.get(f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions?token={write_token}")
        listed = client.get(f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions?token={read_token}")
        forbidden_answer = client.post(f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions/{created.json()['id']}/answer?token={read_token}")
        answered = client.post(f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions/{created.json()['id']}/answer?token={moderate_token}")
        dismissed = client.post(f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions/{created.json()['id']}/dismiss?token={moderate_token}")

    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["external_lesson_id"] == "ext-stage24"
    assert payload["student_name"] == "Aidos"
    assert payload["translated_text_ru"] == "[ru mock] Massiv degen ne?"
    assert forbidden_list.status_code == 403
    assert listed.status_code == 200, listed.text
    assert listed.json()["questions"][0]["id"] == payload["id"]
    assert forbidden_answer.status_code == 403
    assert answered.status_code == 200, answered.text
    assert answered.json()["status"] == "answered"
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["status"] == "dismissed"


def test_v1_question_ws_receives_enriched_question_created(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "question-ws.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        read_token = _token("teacher-1", "teacher", lesson["lesson_id"], ["question:read"])
        write_token = _token("student-1", "student", lesson["lesson_id"], ["question:write"])
        with client.websocket_connect(f"/ws/v1/lessons/{lesson['lesson_id']}/questions?token={read_token}") as websocket:
            response = client.post(
                f"/api/v1/integration/lessons/{lesson['lesson_id']}/questions/text?token={write_token}",
                json={"student_name": "Aidos", "source_language": "ru", "text": "What is an array?"},
            )
            event = websocket.receive_json()

    assert response.status_code == 201, response.text
    assert event["event"] == "question_created"
    assert event["version"] == "1.0"
    assert event["lesson_id"] == lesson["lesson_id"]
    assert event["external_lesson_id"] == "ext-stage24"
    assert event["question"]["external_lesson_id"] == "ext-stage24"
    assert event["question"]["original_text"] == "What is an array?"


def test_v1_student_voice_question_ws_rejects_missing_or_wrong_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "voice-reject.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        wrong_token = _token("student-1", "student", lesson["lesson_id"], ["question:read"])
        with pytest.raises(WebSocketDisconnect) as missing:
            with client.websocket_connect(f"/ws/v1/lessons/{lesson['lesson_id']}/student-question-audio"):
                pass
        with pytest.raises(WebSocketDisconnect) as forbidden:
            with client.websocket_connect(f"/ws/v1/lessons/{lesson['lesson_id']}/student-question-audio?token={wrong_token}"):
                pass

    assert missing.value.code == 4401
    assert forbidden.value.code == 4403


def test_v1_student_voice_question_ws_accepts_question_write_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "voice-accept.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        read_token = _token("teacher-1", "teacher", lesson["lesson_id"], ["question:read"])
        write_token = _token("student-1", "student", lesson["lesson_id"], ["question:write"])
        with client.websocket_connect(f"/ws/v1/lessons/{lesson['lesson_id']}/questions?token={read_token}") as questions_ws:
            with client.websocket_connect(f"/ws/v1/lessons/{lesson['lesson_id']}/student-question-audio?token={write_token}") as audio_ws:
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
                audio_ws.send_bytes("voice question".encode())
                audio_ws.send_text(json.dumps({"event": "finish_question"}))
                audio_result = audio_ws.receive_json()
            event = questions_ws.receive_json()

    assert audio_result["event"] == "question_created"
    assert audio_result["version"] == "1.0"
    assert audio_result["question"]["input_type"] == "voice"
    assert audio_result["question"]["recognized_text"] == "voice question"
    assert event["event"] == "question_created"
    assert event["question"]["input_type"] == "voice"


def test_existing_demo_tts_and_questions_still_work(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "demo-regression.db", dev_bypass=True)

    with TestClient(app) as client:
        lesson = _create_demo_lesson(client)
        tts = client.post(f"/api/lessons/{lesson['lesson_id']}/tts/synthesize", json={"text": "Salem", "language": "kk"})
        question = client.post(
            f"/api/lessons/{lesson['lesson_id']}/questions/text",
            json={"student_name": "Student", "source_language": "ru", "text": "Demo question"},
        )

    assert tts.status_code == 200, tts.text
    assert question.status_code == 201, question.text


def test_integration_spec_includes_stage24_sections(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "spec.db")

    with TestClient(app) as client:
        response = client.get("/api/v1/integration/spec", headers=_headers())

    payload = response.json()
    assert response.status_code == 200, response.text
    assert "tts" in payload
    assert "questions" in payload
    assert "question_websocket" in payload
    assert "voice_question_audio_websocket" in payload
    assert "tts:play" in payload["auth"]["scopes"]
    assert "/api/v1/integration/lessons/{lesson_id}/tts/synthesize" in payload["http_endpoints"]
    assert "/ws/v1/lessons/{lesson_id}/student-question-audio" in payload["websocket_endpoints"]


def _app(tmp_path, monkeypatch, db_name: str, *, dev_bypass: bool = False):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("INTEGRATION_AUTH_ENABLED", "true")
    monkeypatch.setenv("INTEGRATION_API_KEYS", "dev-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://python-service.example.test")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage24-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "false" if dev_bypass else "true")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true" if dev_bypass else "false")
    monkeypatch.setenv("TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("STUDENT_QUESTION_STT_PROVIDER", "mock")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("TTS_RATE_LIMIT_PER_MINUTE", "20")
    monkeypatch.setenv("QUESTION_TEXT_RATE_LIMIT_PER_MINUTE", "10")
    monkeypatch.setenv("QUESTION_VOICE_RATE_LIMIT_PER_MINUTE", "5")
    return create_app()


def _headers() -> dict:
    return {"X-Integration-Key": "dev-key"}


def _create_lesson(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/integration/lessons",
        headers=_headers(),
        json={
            "external_lesson_id": "ext-stage24",
            "title": "Stage 24",
            "mode": "mock",
            "stt_provider": "mock",
            "translation_provider": "mock",
            "target_languages": ["kk", "uz", "zh-Hans"],
            "create_zoom_meeting": False,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_demo_lesson(client: TestClient) -> dict:
    response = client.post(
        "/api/lessons",
        json={"title": "Stage 24 Demo", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock", "target_languages": ["kk"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _token(sub: str, role: str, lesson_id: str, scopes: list[str]) -> str:
    return create_access_token(
        {
            "sub": sub,
            "role": role,
            "lesson_id": lesson_id,
            "external_lesson_id": "ext-stage24",
            "scopes": scopes,
        },
        ttl_seconds=3600,
    )
