from fastapi.testclient import TestClient

from app.main import create_app
from app.zoom.models import ZoomMeeting


def test_zoom_lesson_defaults_to_browser_ws_audio_source(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'zoom-browser-default.db').as_posix()}")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        response = client.post("/api/lessons", json={"title": "Zoom default", "mode": "zoom"})

    assert response.status_code == 201
    payload = response.json()
    assert payload["mode"] == "zoom"
    assert payload["audio_source"] == "browser_ws"
    assert payload["browser_audio_status"] == "waiting_for_teacher"


def test_provider_status_hides_rtms_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'providers-no-rtms.db').as_posix()}")
    monkeypatch.delenv("RTMS_UI_ENABLED", raising=False)
    monkeypatch.delenv("RTMS_EXPERIMENTAL_ENABLED", raising=False)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    assert response.status_code == 200
    payload = response.json()
    assert "api" in payload["zoom"]
    assert "meeting_sdk" in payload["zoom"]
    assert "browser_audio" in payload
    assert "rtms" not in payload["zoom"]


def test_real_test_page_is_browser_mic_flow_and_hides_rtms_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'real-browser-flow.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/real-test")

    assert response.status_code == 200
    assert "Real Local Zoom + Browser Mic Test" in response.text
    assert "Zoom RTMS" not in response.text
    assert "Arm RTMS" not in response.text
    assert "Browser Mic WebSocket" in response.text


def test_rtms_actions_return_disabled_response_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'rtms-disabled.db').as_posix()}")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Zoom", "mode": "zoom"}).json()
        response = client.post(f"/api/lessons/{lesson['lesson_id']}/arm-rtms")

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == "HTTP_400"
    assert payload["detail"]["code"] == "RTMS_DISABLED"
    assert "Use Browser Mic WebSocket audio" in payload["detail"]["message"]


def test_teacher_and_dashboard_hide_rtms_panel_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'hidden-rtms-ui.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Teacher", "mode": "mock"}).json()
        teacher = client.get(f"/teacher/{lesson['lesson_id']}")
        dashboard = client.get("/dashboard")

    assert teacher.status_code == 200
    assert "Teacher Microphone Audio" in teacher.text
    assert "Experimental Zoom RTMS" not in teacher.text
    assert dashboard.status_code == 200
    assert "Experimental Zoom RTMS" not in dashboard.text


def test_integration_zoom_lesson_defaults_to_browser_ws_audio_source(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'integration-browser-default.db').as_posix()}")
    monkeypatch.setenv("INTEGRATION_AUTH_ENABLED", "false")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/integration/lessons",
            json={
                "external_lesson_id": "ext-browser-default",
                "title": "Integration Zoom",
                "mode": "zoom",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk"],
            },
        )

    assert response.status_code == 201
    assert response.json()["audio_source"] == "browser_ws"


class FakeZoomAPIClient:
    async def create_meeting(self, title: str) -> ZoomMeeting:
        return ZoomMeeting(
            meeting_id="123456789",
            meeting_uuid="uuid_stage15a",
            join_url="https://zoom.us/j/123456789?pwd=pass123",
            start_url="https://zoom.us/s/123456789?zak=secret",
            topic=title,
            created_at="2026-05-13T10:00:00Z",
            password="pass123",
        )
