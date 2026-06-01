from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db.models import TranscriptSegment
from app.integration.callbacks import IntegrationCallbackSender, build_callback_payload
from app.main import create_app
from app.zoom.models import ZoomMeeting


def test_integration_endpoints_require_api_key_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'auth-required.db').as_posix()}")
    monkeypatch.setenv("INTEGRATION_AUTH_ENABLED", "true")
    monkeypatch.setenv("INTEGRATION_API_KEYS", "dev-key")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/v1/integration/providers/status")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "HTTP_401"


def test_integration_endpoints_allow_access_when_auth_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'auth-disabled.db').as_posix()}")
    monkeypatch.setenv("INTEGRATION_AUTH_ENABLED", "false")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/v1/integration/providers/status")

    assert response.status_code == 200
    assert "stt" in response.json()


def test_create_integration_lesson_stores_external_lesson_id(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "create.db")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/integration/lessons",
            headers=_headers(),
            json={
                "external_lesson_id": "csharp-lesson-123",
                "external_course_id": "course-456",
                "external_teacher_id": "teacher-789",
                "external_tenant_id": "tenant-1",
                "title": "C# Arrays Lesson",
                "mode": "mock",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk", "uz", "zh-Hans"],
                "create_zoom_meeting": False,
                "callback_url": "https://csharp.example.test/webhooks/translation",
            },
        )

    payload = response.json()
    assert response.status_code == 201
    assert payload["external_lesson_id"] == "csharp-lesson-123"
    assert payload["student"]["captions_websocket_url"].endswith(f"/ws/v1/lessons/{payload['lesson_id']}/captions")

    with app.state.database.session_factory() as session:
        from app.db.repositories import LessonRepository

        lesson = LessonRepository(session).get(payload["lesson_id"])
    assert lesson.external_lesson_id == "csharp-lesson-123"
    assert lesson.external_course_id == "course-456"


def test_get_lesson_by_external_lesson_id_works(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "external.db")

    with TestClient(app) as client:
        created = _create_integration_lesson(client)
        response = client.get("/api/v1/integration/lessons/by-external/ext-lesson", headers=_headers())

    assert response.status_code == 200
    assert response.json()["lesson_id"] == created["lesson_id"]
    assert response.json()["external_lesson_id"] == "ext-lesson"


def test_student_embed_config_does_not_include_start_url(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_ID", "sdk-client")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_SECRET", "sdk-secret")
    app = _app(tmp_path, monkeypatch, "embed.db")
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        created = _create_integration_lesson(client, mode="zoom", create_zoom_meeting=True)
        response = client.get(f"/api/v1/integration/lessons/{created['lesson_id']}/zoom/embed-config?user_name=Student", headers=_headers())

    payload = response.json()
    assert response.status_code == 200
    assert payload["role"] == 0
    assert "signature" in payload
    assert "start_url" not in payload


def test_arm_rtms_via_integration_endpoint_works(tmp_path, monkeypatch):
    monkeypatch.setenv("RTMS_EXPERIMENTAL_ENABLED", "true")
    app = _app(tmp_path, monkeypatch, "arm.db")
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        created = _create_integration_lesson(client, mode="zoom", create_zoom_meeting=True)
        response = client.post(f"/api/v1/integration/lessons/{created['lesson_id']}/arm-rtms", headers=_headers())

    assert response.status_code == 200
    assert response.json()["armed"] is True
    assert response.json()["rtms_status"] == "waiting_for_meeting"


def test_status_endpoint_returns_stable_dto(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "status.db")

    with TestClient(app) as client:
        created = _create_integration_lesson(client)
        response = client.get(f"/api/v1/integration/lessons/{created['lesson_id']}/status", headers=_headers())

    payload = response.json()
    assert response.status_code == 200
    assert payload["external_lesson_id"] == "ext-lesson"
    assert "stt" in payload
    assert "translation" in payload
    assert "captions" in payload
    assert "latency_ms" in payload


def test_transcript_and_export_endpoints_work_through_integration_namespace(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "exports.db")

    with TestClient(app) as client:
        created = _create_integration_lesson(client)
        _add_transcript(app, created["lesson_id"])
        transcript = client.get(f"/api/v1/integration/lessons/{created['lesson_id']}/transcript", headers=_headers())
        srt = client.get(f"/api/v1/integration/lessons/{created['lesson_id']}/exports/srt?lang=kk", headers=_headers())

    assert transcript.status_code == 200
    assert transcript.json()["segments"][0]["translations"]["kk"] == "C# массивтері"
    assert srt.status_code == 200
    assert "C# массивтері" in srt.text


def test_captions_ws_accepts_integration_key_query_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "ws.db")

    with TestClient(app) as client:
        created = _create_integration_lesson(client)
        with client.websocket_connect(f"/ws/v1/lessons/{created['lesson_id']}/captions?integration_key=dev-key") as websocket:
            websocket.send_text("ping")

    assert app.state.caption_hub.connected_count(created["lesson_id"]) == 0


def test_callback_payload_builder_includes_contract_fields():
    payload = build_callback_payload(
        event="caption.final",
        lesson_id="lesson_1",
        external_lesson_id="ext_1",
        data={"original_text": "C#"},
    )

    assert payload["event"] == "caption.final"
    assert payload["version"] == "1.0"
    assert payload["lesson_id"] == "lesson_1"
    assert payload["external_lesson_id"] == "ext_1"
    assert payload["data"]["original_text"] == "C#"


@pytest.mark.asyncio
async def test_callback_failure_does_not_crash():
    sender = IntegrationCallbackSender(callback_secret="secret", max_attempts=2, backoff_seconds=0, client_factory=lambda: FailingAsyncClient())

    result = await sender.send("https://csharp.example.test/webhook", {"event": "caption.final"})

    assert result["ok"] is False
    assert result["attempts"] == 2
    assert "boom" in result["error"]


def test_secrets_redacted_in_integration_status(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "secret-elevenlabs-key")
    app = _app(tmp_path, monkeypatch, "redacted.db")

    with TestClient(app) as client:
        response = client.get("/api/v1/integration/providers/status", headers=_headers())

    assert response.status_code == 200
    assert "secret-elevenlabs-key" not in response.text


def test_machine_readable_integration_spec_endpoint(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "spec.db")

    with TestClient(app) as client:
        response = client.get("/api/v1/integration/spec", headers=_headers())

    payload = response.json()
    assert response.status_code == 200
    assert payload["version"] == "1.0"
    assert "/api/v1/integration/lessons" in payload["http_endpoints"]
    assert "/ws/v1/lessons/{lesson_id}/captions" in payload["websocket_endpoints"]


def _app(tmp_path, monkeypatch, db_name: str):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("INTEGRATION_AUTH_ENABLED", "true")
    monkeypatch.setenv("INTEGRATION_API_KEYS", "dev-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://python-service.example.test")
    return create_app()


def _headers() -> dict:
    return {"X-Integration-Key": "dev-key"}


def _create_integration_lesson(client: TestClient, mode: str = "mock", create_zoom_meeting: bool = False) -> dict:
    response = client.post(
        "/api/v1/integration/lessons",
        headers=_headers(),
        json={
            "external_lesson_id": "ext-lesson",
            "title": "Integration Lesson",
            "mode": mode,
            "stt_provider": "mock",
            "translation_provider": "mock",
            "target_languages": ["kk", "uz", "zh-Hans"],
            "create_zoom_meeting": create_zoom_meeting,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _add_transcript(app, lesson_id: str) -> None:
    with app.state.database.session_factory() as session:
        session.add(
            TranscriptSegment(
                lesson_id=lesson_id,
                original_text="Сегодня изучаем C# массивы",
                original_text_raw="Сегодня изучаем си шарп массивы",
                original_text_normalized="Сегодня изучаем C# массивы",
                translations_json='{"kk": "C# массивтері"}',
                start_time=datetime(2026, 5, 9, 10, 0, 0),
                end_time=datetime(2026, 5, 9, 10, 0, 2),
                is_final=True,
                provider_stt="mock",
                provider_translator="mock",
            )
        )
        session.commit()


class FakeZoomAPIClient:
    async def create_meeting(self, title: str) -> ZoomMeeting:
        return ZoomMeeting(
            meeting_id="123456789",
            meeting_uuid="uuid_123",
            join_url="https://zoom.us/j/123456789?pwd=pass123",
            start_url="https://zoom.us/s/123456789?zak=secret",
            topic=title,
            created_at="2026-05-09T10:00:00Z",
            password="pass123",
        )


class FailingAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, json, headers, timeout):
        raise RuntimeError("boom")
