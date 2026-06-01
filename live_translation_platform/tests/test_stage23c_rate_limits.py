import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.security.tokens import create_access_token


def test_tts_rate_limit_triggers_429(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-rate.db", tts_limit=1)

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        first = _tts(client, lesson["lesson_id"], token=_token("student-1", lesson["lesson_id"], ["tts:play"]))
        second = _tts(client, lesson["lesson_id"], token=_token("student-1", lesson["lesson_id"], ["tts:play"]))

    assert first.status_code == 200, first.text
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "TTS_RATE_LIMITED"
    assert second.json()["detail"]["message"] == "Too many requests, please wait."


def test_tts_load_test_rate_limit_bypass_requires_dev_load_test_gate(tmp_path, monkeypatch):
    app = _app(
        tmp_path,
        monkeypatch,
        "tts-load-bypass.db",
        tts_limit=1,
        tts_load_test_bypass=True,
        app_env="development",
        enable_load_test_endpoints=True,
    )

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _token("student-1", lesson["lesson_id"], ["tts:play"])
        first = _tts(client, lesson["lesson_id"], token=token, headers=_load_test_bypass_headers())
        second = _tts(client, lesson["lesson_id"], token=token, headers=_load_test_bypass_headers())

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text


def test_tts_load_test_rate_limit_bypass_is_ignored_when_gate_disabled(tmp_path, monkeypatch):
    app = _app(
        tmp_path,
        monkeypatch,
        "tts-load-bypass-disabled.db",
        tts_limit=1,
        tts_load_test_bypass=True,
        app_env="development",
        enable_load_test_endpoints=False,
    )

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _token("student-1", lesson["lesson_id"], ["tts:play"])
        first = _tts(client, lesson["lesson_id"], token=token, headers=_load_test_bypass_headers())
        second = _tts(client, lesson["lesson_id"], token=token, headers=_load_test_bypass_headers())

    assert first.status_code == 200, first.text
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "TTS_RATE_LIMITED"


def test_tts_load_test_rate_limit_bypass_is_ignored_in_production(tmp_path, monkeypatch):
    app = _app(
        tmp_path,
        monkeypatch,
        "tts-load-bypass-prod.db",
        tts_limit=1,
        tts_load_test_bypass=True,
        app_env="production",
        enable_load_test_endpoints=True,
    )

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _token("student-1", lesson["lesson_id"], ["tts:play"])
        first = _tts(client, lesson["lesson_id"], token=token, headers=_load_test_bypass_headers())
        second = _tts(client, lesson["lesson_id"], token=token, headers=_load_test_bypass_headers())

    assert first.status_code == 200, first.text
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "TTS_RATE_LIMITED"


def test_text_question_rate_limit_triggers_429(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "question-rate.db", question_text_limit=1)

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        first = _text_question(client, lesson["lesson_id"], student_id="student-1")
        second = _text_question(client, lesson["lesson_id"], student_id="student-1")

    assert first.status_code == 201, first.text
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "QUESTION_RATE_LIMITED"
    assert second.json()["detail"]["message"] == "Too many requests, please wait."


def test_voice_question_ws_rate_limit_triggers_error_and_clean_close(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "voice-rate.db", question_voice_limit=0)

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/student-question-audio") as audio_ws:
            event = audio_ws.receive_json()
            with pytest.raises(WebSocketDisconnect):
                audio_ws.receive_json()

    assert event["event"] == "question_error"
    assert event["code"] == "QUESTION_RATE_LIMITED"
    assert event["error"] == "Too many requests, please wait."


def test_rate_limit_disabled_allows_repeated_tts_requests(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "disabled-rate.db", rate_enabled=False, tts_limit=1)

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        first = _tts(client, lesson["lesson_id"])
        second = _tts(client, lesson["lesson_id"])

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text


def test_rate_limit_keys_text_questions_by_lesson_and_student(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "student-key-rate.db", question_text_limit=1)

    with TestClient(app) as client:
        lesson_a = _create_lesson(client)
        lesson_b = _create_lesson(client)
        first = _text_question(client, lesson_a["lesson_id"], student_id="same-student")
        same_student_same_lesson = _text_question(client, lesson_a["lesson_id"], student_id="same-student")
        other_student_same_lesson = _text_question(client, lesson_a["lesson_id"], student_id="other-student")
        same_student_other_lesson = _text_question(client, lesson_b["lesson_id"], student_id="same-student")

    assert first.status_code == 201, first.text
    assert same_student_same_lesson.status_code == 429
    assert other_student_same_lesson.status_code == 201, other_student_same_lesson.text
    assert same_student_other_lesson.status_code == 201, same_student_other_lesson.text


def test_rate_limit_keys_tts_by_lesson_and_token_subject(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "token-key-rate.db", tts_limit=1)

    with TestClient(app) as client:
        lesson_a = _create_lesson(client)
        lesson_b = _create_lesson(client)
        student_a_token = _token("student-a", lesson_a["lesson_id"], ["tts:play"])
        student_b_token = _token("student-b", lesson_a["lesson_id"], ["tts:play"])
        student_a_lesson_b_token = _token("student-a", lesson_b["lesson_id"], ["tts:play"])
        first = _tts(client, lesson_a["lesson_id"], token=student_a_token)
        same_token_same_lesson = _tts(client, lesson_a["lesson_id"], token=student_a_token)
        other_token_same_lesson = _tts(client, lesson_a["lesson_id"], token=student_b_token)
        same_subject_other_lesson = _tts(client, lesson_b["lesson_id"], token=student_a_lesson_b_token)

    assert first.status_code == 200, first.text
    assert same_token_same_lesson.status_code == 429
    assert other_token_same_lesson.status_code == 200, other_token_same_lesson.text
    assert same_subject_other_lesson.status_code == 200, same_subject_other_lesson.text


def test_rate_limit_docs_and_ui_show_friendly_message(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "rate-ui.db")

    with TestClient(app) as client:
        tts_js = client.get("/static/student_tts.js")
        questions_js = client.get("/static/student_questions.js")
    env = open(".env.example", encoding="utf-8").read()
    readme = open("README.md", encoding="utf-8").read()
    architecture = open("docs/ARCHITECTURE.md", encoding="utf-8").read()

    assert "Too many requests, please wait." in tts_js.text
    assert "Too many requests, please wait." in questions_js.text
    assert "RATE_LIMIT_ENABLED=true" in env
    assert "TTS_RATE_LIMIT_PER_MINUTE=20" in env
    assert "Production Deployment" in readme
    assert "Stage 23C" in architecture
    assert "single-worker" in architecture


def _app(
    tmp_path,
    monkeypatch,
    db_name: str,
    *,
    rate_enabled: bool = True,
    tts_limit: int = 20,
    question_text_limit: int = 10,
    question_voice_limit: int = 5,
    tts_load_test_bypass: bool = False,
    app_env: str = "development",
    enable_load_test_endpoints: bool = False,
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("ENABLE_LOAD_TEST_ENDPOINTS", "true" if enable_load_test_endpoints else "false")
    monkeypatch.setenv("TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("STT_PROVIDER", "mock")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage23c-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true" if rate_enabled else "false")
    monkeypatch.setenv("TTS_RATE_LIMIT_PER_MINUTE", str(tts_limit))
    monkeypatch.setenv("TTS_LOAD_TEST_BYPASS_RATE_LIMIT", "true" if tts_load_test_bypass else "false")
    monkeypatch.setenv("QUESTION_TEXT_RATE_LIMIT_PER_MINUTE", str(question_text_limit))
    monkeypatch.setenv("QUESTION_VOICE_RATE_LIMIT_PER_MINUTE", str(question_voice_limit))
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post(
        "/api/lessons",
        json={"title": "Stage 23C", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock", "target_languages": ["kk"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _tts(client: TestClient, lesson_id: str, token: str | None = None, headers: dict | None = None):
    suffix = f"?token={token}" if token else ""
    return client.post(
        f"/api/lessons/{lesson_id}/tts/synthesize{suffix}",
        headers=headers or {},
        json={"text": "Сәлем", "language": "kk"},
    )


def _text_question(client: TestClient, lesson_id: str, student_id: str):
    return client.post(
        f"/api/lessons/{lesson_id}/questions/text",
        json={"student_id": student_id, "student_name": "Student", "source_language": "ru", "text": "Question?"},
    )


def _token(sub: str, lesson_id: str, scopes: list[str]) -> str:
    return create_access_token(
        {"sub": sub, "role": "student", "lesson_id": lesson_id, "external_lesson_id": "ext-stage23c", "scopes": scopes},
        ttl_seconds=3600,
    )


def _load_test_bypass_headers() -> dict:
    return {"X-TTS-Load-Test-Bypass-Rate-Limit": "true"}
