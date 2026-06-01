from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.rtms import RTMSStatus
from app.zoom.models import ZoomMeeting


def test_real_test_page_returns_200(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'real-page.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/real-test")

    assert response.status_code == 200
    assert "Real Local Zoom + Browser Mic Test" in response.text


def test_real_test_readiness_returns_grouped_zoom_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'readiness.db').as_posix()}")
    monkeypatch.setenv("ZOOM_RTMS_CLIENT_ID", "")
    monkeypatch.setenv("ZOOM_RTMS_CLIENT_SECRET", "")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_ID", "")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_SECRET", "")

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/api/real-test/readiness")

    assert response.status_code == 200
    payload = response.json()
    assert "zoom" in payload
    assert "rtms" not in payload["zoom"]
    assert payload["browser_audio"]["ready"] is True
    assert "ZOOM_MEETING_SDK_KEY or ZOOM_SDK_KEY" in payload["zoom"]["meeting_sdk"]["missing"]
    assert "ZOOM_MEETING_SDK_SECRET or ZOOM_SDK_SECRET" in payload["zoom"]["meeting_sdk"]["missing"]


def test_real_test_page_shows_meeting_sdk_missing_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'real-page-sdk-warning.db').as_posix()}")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_ID", "")
    monkeypatch.setenv("ZOOM_MEETING_SDK_CLIENT_SECRET", "")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/real-test")

    assert response.status_code == 200
    assert "Zoom video unavailable: Meeting SDK credentials missing." in response.text


def test_real_test_create_lesson_creates_zoom_lesson_and_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'create-real.db').as_posix()}")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        response = client.post(
            "/api/real-test/create-lesson",
            json={
                "title": "Real test",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "target_languages": ["kk"],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["lesson"]["mode"] == "zoom"
    assert payload["lesson"]["zoom"]["meeting_id"] == "123456789"
    assert payload["lesson"]["zoom"]["meeting_uuid"] == "uuid_123"
    assert payload["lesson"]["zoom"]["password"] == "pass123"
    assert payload["lesson"]["zoom"]["join_url"]
    assert payload["lesson"]["zoom"]["start_url"]
    assert payload["real_test_id"].startswith("real_")


def test_arm_rtms_sets_lesson_state_to_armed_waiting(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'arm.db').as_posix()}")
    monkeypatch.setenv("RTMS_EXPERIMENTAL_ENABLED", "true")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Zoom", "mode": "zoom"}).json()
        response = client.post(f"/api/lessons/{lesson['lesson_id']}/arm-rtms")
        status = client.get(f"/api/lessons/{lesson['lesson_id']}/rtms").json()

    assert response.status_code == 200
    assert response.json()["rtms_armed"] is True
    assert status["rtms_status"] == RTMSStatus.WAITING_FOR_MEETING


def test_webhook_after_arm_records_webhook_and_prevents_duplicate_clients(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'webhook-after-arm.db').as_posix()}")
    monkeypatch.setenv("ZOOM_RTMS_ENABLED", "false")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Zoom", "mode": "zoom"}).json()
        client.post(f"/api/lessons/{lesson['lesson_id']}/arm-rtms")
        payload = {
            "event": "meeting.rtms_started",
            "payload": {"rtms_stream_id": "stream_1", "object": {"id": lesson["zoom"]["meeting_id"], "uuid": lesson["zoom"]["meeting_uuid"]}},
        }
        first = client.post("/api/zoom/webhook", json=payload)
        second = client.post("/api/zoom/webhook", json=payload)
        status = client.get(f"/api/lessons/{lesson['lesson_id']}/rtms").json()

    assert first.status_code == 200
    assert second.status_code == 200
    assert status["rtms_stream_id"] == "stream_1"
    assert len(app.state.rtms_manager.clients) <= 1


def test_webhook_before_arm_stores_webhook_received_when_auto_start_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'webhook-before-arm.db').as_posix()}")
    monkeypatch.setenv("ZOOM_RTMS_ENABLED", "true")
    monkeypatch.setenv("RTMS_AUTO_START_PIPELINE_ON_WEBHOOK", "false")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Zoom", "mode": "zoom"}).json()
        response = client.post(
            "/api/zoom/webhook",
            json={
                "event": "meeting.rtms_started",
                "payload": {"rtms_stream_id": "stream_before", "object": {"id": lesson["zoom"]["meeting_id"]}},
            },
        )
        status = client.get(f"/api/lessons/{lesson['lesson_id']}/rtms").json()

    assert response.status_code == 200
    assert response.json()["status"] == "stored"
    assert status["rtms_status"] == RTMSStatus.WEBHOOK_RECEIVED
    assert status["rtms_stream_id"] == "stream_before"


def test_diagnostics_endpoint_returns_status_counters_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'diagnostics.db').as_posix()}")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Zoom", "mode": "zoom"}).json()
        response = client.get(f"/api/lessons/{lesson['lesson_id']}/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["lesson"]["lesson_id"] == lesson["lesson_id"]
    assert "rtms" in payload
    assert "latest_errors" in payload
    assert "latest_captions" in payload


def test_diagnostics_websocket_receives_rtms_status(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'diagnostics-ws.db').as_posix()}")
    monkeypatch.setenv("ZOOM_RTMS_ENABLED", "false")
    app = create_app()
    app.state.zoom_api_client = FakeZoomAPIClient()

    with TestClient(app) as client:
        lesson = client.post("/api/lessons", json={"title": "Zoom", "mode": "zoom"}).json()
        with client.websocket_connect(f"/ws/lessons/{lesson['lesson_id']}/diagnostics") as websocket:
            client.post(f"/api/lessons/{lesson['lesson_id']}/arm-rtms")
            event = websocket.receive_json()

    assert event["event"] in {"rtms_status", "readiness_update"}
    assert event["lesson_id"] == lesson["lesson_id"]


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
