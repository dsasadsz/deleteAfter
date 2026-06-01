import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import create_app
from app.production import sanitize_for_log
from app.runtime import shutdown_runtime


def test_health_live_returns_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'live.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"
    assert response.json()["env"] == "development"


def test_health_ready_returns_db_and_config_status(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'ready.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "ready"
    assert payload["database_status"] == "ok"
    assert payload["database"]["type"] == "sqlite"
    assert payload["database"]["production_ready"] is True
    assert payload["config"] == "ok"
    assert "providers" in payload


def test_production_config_validation_catches_missing_public_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'prod-check.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("ZOOM_WEBHOOK_SECRET_TOKEN", raising=False)
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION", "false")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/system/config-check")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "error"
    assert "PUBLIC_BASE_URL" in payload["missing"]
    assert "ZOOM_WEBHOOK_SECRET_TOKEN" not in payload["missing"]


def test_production_config_requires_zoom_webhook_secret_when_signature_required(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'prod-webhook-secret.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.test")
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("ENABLE_OPENAPI_DOCS", "false")
    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "false")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("WEBSOCKET_AUTH_REQUIRED_IN_PRODUCTION", "false")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION", "true")
    monkeypatch.delenv("ZOOM_WEBHOOK_SECRET_TOKEN", raising=False)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/system/config-check")
        ready_response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert "ZOOM_WEBHOOK_SECRET_TOKEN" in response.json()["missing"]
    assert ready_response.json()["status"] == "not_ready"


def test_request_id_middleware_adds_request_id_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'request-id.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/live", headers={"X-Request-ID": "req_stage13"})

    assert response.headers["x-request-id"] == "req_stage13"


def test_error_response_includes_request_id(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'error-id.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/lessons/missing_lesson", headers={"X-Request-ID": "req_error"})

    payload = response.json()
    assert response.status_code == 404
    assert payload["request_id"] == "req_error"
    assert payload["error"]["code"] == "HTTP_404"


def test_provider_status_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redact-status.db').as_posix()}")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "secret-elevenlabs-key")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    assert response.status_code == 200
    assert "secret-elevenlabs-key" not in response.text


def test_metrics_endpoint_returns_core_counters(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'metrics.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/metrics")

    payload = response.json()
    assert response.status_code == 200
    assert "active_lessons" in payload
    assert "active_rtms_sessions" in payload
    assert "active_websockets" in payload
    assert "captions_sent_total" in payload


def test_debug_endpoint_disabled_in_production(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'prod-debug.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "false")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/system/tasks")

    assert response.status_code == 403


def test_sanitize_for_log_removes_secret_like_fields():
    payload = sanitize_for_log(
        {
            "api_key": "abc",
            "nested": {"token": "def", "safe": "ok"},
            "zoom_start_url": "https://zoom.us/s/123?zak=secret",
            "items": [{"client_secret": "ghi"}],
        }
    )

    assert payload["api_key"] == "[redacted]"
    assert payload["nested"]["token"] == "[redacted]"
    assert payload["nested"]["safe"] == "ok"
    assert payload["zoom_start_url"] == "[redacted]"
    assert payload["items"][0]["client_secret"] == "[redacted]"


def test_graceful_shutdown_calls_runtime_managers():
    session_manager = FakeSessionManager()
    rtms_manager = FakeRTMSManager()
    app = SimpleNamespace(state=SimpleNamespace(session_manager=session_manager, rtms_manager=rtms_manager, settings=SimpleNamespace(worker_shutdown_timeout_seconds=1)))

    asyncio.run(shutdown_runtime(app))

    assert session_manager.stopped == ["lesson_a"]
    assert rtms_manager.stopped == ["lesson_b"]


class FakeSessionManager:
    def __init__(self):
        self.sessions = {"lesson_a": object()}
        self.stopped = []

    async def stop(self, lesson_id: str) -> None:
        self.stopped.append(lesson_id)


class FakeRTMSManager:
    def __init__(self):
        self.clients = {"lesson_b": object()}
        self.stopped = []

    async def stop_lesson(self, lesson_id: str) -> None:
        self.stopped.append(lesson_id)
