import pytest
from fastapi.testclient import TestClient

from app.db.models import Lesson
from app.main import create_app
from app.zoom.meeting_sdk import (
    MeetingSDKConfig,
    ZoomMeetingSDKConfigurationError,
    ZoomMeetingSDKSignatureService,
)
from app.zoom.models import ZoomMeeting


def test_meeting_sdk_signature_service_refuses_missing_credentials():
    service = ZoomMeetingSDKSignatureService(MeetingSDKConfig(client_id="", client_secret=""))

    with pytest.raises(ZoomMeetingSDKConfigurationError, match="Zoom Meeting SDK credentials"):
        service.generate_signature("123456789", role=0)


def test_meeting_sdk_signature_is_non_empty_and_deterministic_with_fixed_timestamp():
    service = ZoomMeetingSDKSignatureService(MeetingSDKConfig(client_id="client_id", client_secret="secret"))

    first = service.generate_signature("123456789", role=0, now_ms=1_700_000_000_000)
    second = service.generate_signature("123456789", role=0, now_ms=1_700_000_000_000)

    assert first
    assert first == second
    assert first.count(".") == 2


def test_student_embed_config_for_mock_lesson_returns_mock_mode(tmp_path, monkeypatch):
    db_path = tmp_path / "mock.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    app = create_app()
    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Mock", "mode": "mock"}).json()
        response = client.get(f"/api/lessons/{lesson['lesson_id']}/zoom/embed-config")

    assert response.status_code == 200
    assert response.json()["mode"] == "mock"
    assert "start_url" not in response.json()


def test_student_embed_config_for_zoom_lesson_without_sdk_credentials_returns_clear_error(tmp_path, monkeypatch):
    db_path = tmp_path / "zoom-no-sdk.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_ID", "")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_SECRET", "")

    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()
    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Zoom", "mode": "zoom"}).json()
        response = client.get(f"/api/lessons/{lesson['lesson_id']}/zoom/embed-config")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "ZOOM_SDK_NOT_CONFIGURED"
    assert "Zoom Meeting SDK credentials are not configured" in response.json()["detail"]["message"]
    assert response.json()["detail"]["details"]["has_zoom_meeting_id"] is True
    assert response.json()["detail"]["details"]["has_sdk_key"] is False
    assert "ZOOM_MEETING_SDK_KEY or ZOOM_SDK_KEY" in response.json()["detail"]["details"]["missing"]


def test_student_embed_config_for_zoom_lesson_without_meeting_returns_structured_error(tmp_path, monkeypatch):
    db_path = tmp_path / "zoom-no-meeting.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_ID", "client_id")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_SECRET", "secret")

    app = create_app()
    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Mock metadata", "mode": "mock"}).json()
        with app.state.database.session_factory() as session:
            db_lesson = session.get(Lesson, lesson["lesson_id"])
            db_lesson.mode = "zoom"
            db_lesson.zoom_meeting_id = ""
            db_lesson.zoom_password = ""
            session.commit()
        response = client.get(f"/api/lessons/{lesson['lesson_id']}/zoom/embed-config")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "LESSON_ZOOM_NOT_READY"
    assert response.json()["detail"]["details"]["has_zoom_meeting_id"] is False
    assert response.json()["detail"]["details"]["has_sdk_key"] is True


def test_student_embed_config_for_zoom_lesson_with_sdk_credentials_omits_start_url(tmp_path, monkeypatch):
    db_path = tmp_path / "zoom-sdk.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_ID", "client_id")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_SECRET", "secret")

    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()
    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Zoom", "mode": "zoom"}).json()
        response = client.get(f"/api/lessons/{lesson['lesson_id']}/zoom/embed-config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "zoom"
    assert payload["meeting_number"] == "123456789"
    assert payload["role"] == 0
    assert payload["signature"]
    assert payload["sdk_key_or_client_id"] == "client_id"
    assert payload["password"] == "pass123"
    assert "start_url" not in payload


def test_student_embed_config_accepts_meeting_sdk_key_secret_aliases(tmp_path, monkeypatch):
    db_path = tmp_path / "zoom-sdk-aliases.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_ID", "")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_SECRET", "")
    monkeypatch.setenv("ZOOM_MEETING_SDK_SDK_KEY", "")
    monkeypatch.setenv("ZOOM_MEETING_SDK_KEY", "alias_key")
    monkeypatch.setenv("ZOOM_MEETING_SDK_SECRET", "alias_secret")

    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()
    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Zoom aliases", "mode": "zoom"}).json()
        response = client.get(f"/api/lessons/{lesson['lesson_id']}/zoom/embed-config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sdk_key_or_client_id"] == "alias_key"
    assert "alias_secret" not in response.text


def test_meeting_sdk_signature_post_endpoint_returns_student_role_config(tmp_path, monkeypatch):
    db_path = tmp_path / "zoom-signature.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_ID", "client_id")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_SECRET", "secret")

    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()
    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Zoom", "mode": "zoom"}).json()
        response = client.post(
            "/api/zoom/meeting-sdk/signature",
            json={"lesson_id": lesson["lesson_id"], "role": 0, "user_name": "Student"},
        )

    assert response.status_code == 200
    assert response.json()["role"] == 0
    assert response.json()["user_name"] == "Student"
    assert "start_url" not in response.json()


def test_student_page_returns_200(tmp_path, monkeypatch):
    db_path = tmp_path / "student.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    app = create_app()
    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Mock", "mode": "mock"}).json()
        response = client.get(f"/student/{lesson['lesson_id']}")

    assert response.status_code == 200
    assert "captionOverlay" in response.text


class FakeZoomAPIClient:
    async def create_meeting(self, title: str) -> ZoomMeeting:
        return ZoomMeeting(
            meeting_id="123456789",
            meeting_uuid="uuid_123",
            join_url="https://zoom.us/j/123456789?pwd=pass123",
            start_url="https://zoom.us/s/123456789?zak=secret",
            topic=title,
            created_at="2026-05-08T10:00:00Z",
            password="pass123",
        )
