from fastapi.testclient import TestClient

from app.main import create_app
from app.zoom.models import ZoomMeeting


def test_mock_lesson_websocket_flow(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    app = create_app()
    with TestClient(app) as client:
        create_response = client.post(
            "/api/lessons",
            json={
                "title": "C# lesson",
                "mode": "mock",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk", "uz", "zh-Hans"],
            },
        )
        assert create_response.status_code == 201
        lesson_id = create_response.json()["lesson_id"]

        with client.websocket_connect(f"/ws/lessons/{lesson_id}/captions") as websocket:
            start_response = client.post(f"/api/lessons/{lesson_id}/start")
            assert start_response.status_code == 200
            payload = websocket.receive_json()
            while not payload["is_final"]:
                payload = websocket.receive_json()

        assert payload["event"] == "caption"
        assert payload["lesson_id"] == lesson_id
        assert set(payload["translations"]) == {"kk", "uz", "zh-Hans"}
        assert "latency_ms" in payload


def test_create_mock_lesson_still_uses_mock_zoom(tmp_path, monkeypatch):
    db_path = tmp_path / "mock.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/lessons",
            json={
                "title": "C# mock lesson",
                "mode": "mock",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk", "uz", "zh-Hans"],
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["mode"] == "mock"
    assert payload["zoom"]["meeting_id"]
    assert payload["zoom"]["topic"] == "C# mock lesson"


def test_create_zoom_lesson_uses_zoom_client_and_returns_nested_zoom(tmp_path, monkeypatch):
    db_path = tmp_path / "zoom.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    class FakeZoomAPIClient:
        async def create_meeting(self, title: str) -> ZoomMeeting:
            return ZoomMeeting(
                meeting_id="123456789",
                meeting_uuid="uuid_123",
                join_url="https://zoom.us/j/123456789",
                start_url="https://zoom.us/s/123456789?zak=secret",
                topic=title,
                created_at="2026-05-08T10:00:00Z",
            )

    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        response = client.post(
            "/api/lessons",
            json={
                "title": "C# lesson",
                "mode": "zoom",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk", "uz", "zh-Hans"],
            },
        )
        payload = response.json()
        start_response = client.post(f"/api/lessons/{payload['lesson_id']}/start")

    assert response.status_code == 201
    assert payload["mode"] == "zoom"
    assert payload["zoom"]["meeting_id"] == "123456789"
    assert payload["zoom"]["meeting_uuid"] == "uuid_123"
    assert payload["zoom"]["topic"] == "C# lesson"
    assert start_response.status_code == 200
    assert start_response.json()["rtms_status"] == "waiting_for_meeting"


def test_create_zoom_lesson_without_credentials_returns_clear_error(tmp_path, monkeypatch):
    db_path = tmp_path / "missing-creds.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ZOOM_ACCOUNT_ID", "")
    monkeypatch.setenv("ZOOM_CLIENT_ID", "")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "")

    app = create_app()
    with TestClient(app) as client:
        response = client.post(
            "/api/lessons",
            json={
                "title": "C# lesson",
                "mode": "zoom",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk", "uz", "zh-Hans"],
            },
        )

    assert response.status_code == 400
    assert "ZOOM_ACCOUNT_ID" in response.json()["detail"]
