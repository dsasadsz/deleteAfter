from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_configured_quota_appears_in_provider_status(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'quota-status.db').as_posix()}")
    monkeypatch.setenv("AZURE_STT_MAX_CONCURRENT_SESSIONS", "100")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/providers/status")

    payload = response.json()
    assert response.status_code == 200
    assert payload["stt"]["azure"]["quotas"]["stt_concurrent_limit"] == 100
    assert payload["stt"]["azure"]["quotas"]["source"] == "config"
    assert payload["stt"]["azure"]["recommendation"] == "ok"


def test_missing_quota_is_reported_as_unknown():
    from app.providers.quotas import provider_quota_snapshot

    snapshot = provider_quota_snapshot(Settings(), "stt", "cartesia")

    assert snapshot["stt_concurrent_limit"] is None
    assert snapshot["source"] == "unknown"


def test_429_error_is_classified_as_rate_limit():
    from app.providers.quotas import classify_provider_error

    assert classify_provider_error(RuntimeError("Azure TTS request failed with status 429")) == "rate_limit"
    assert classify_provider_error(TimeoutError("provider timed out")) == "timeout"
    assert classify_provider_error(RuntimeError("unauthorized 401 invalid key")) == "auth"
    assert classify_provider_error(RuntimeError("quota exceeded")) == "quota"
    assert classify_provider_error(ConnectionError("websocket disconnected")) == "disconnected"


def test_active_stream_count_compared_to_quota_near_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'quota-near.db').as_posix()}")
    monkeypatch.setenv("AZURE_STT_MAX_CONCURRENT_SESSIONS", "10")
    app = create_app()
    app.state.session_manager.sessions = {f"lesson_{index}": FakeSession() for index in range(8)}

    with TestClient(app) as client:
        response = client.get("/api/providers/status?live=true")

    azure = response.json()["stt"]["azure"]
    assert azure["runtime"]["active_stt_streams"] == 8
    assert azure["recommendation"] == "near_limit"


def test_active_stream_count_over_quota_is_over_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'quota-over.db').as_posix()}")
    monkeypatch.setenv("AZURE_STT_MAX_CONCURRENT_SESSIONS", "3")
    app = create_app()
    app.state.session_manager.sessions = {f"lesson_{index}": FakeSession() for index in range(4)}

    with TestClient(app) as client:
        response = client.get("/api/providers/status?live=true")

    assert response.json()["stt"]["azure"]["recommendation"] == "over_limit"


def test_provider_status_runtime_includes_rate_limit_error_without_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'quota-error.db').as_posix()}")
    monkeypatch.setenv("AZURE_TTS_MAX_REQUESTS_PER_SECOND", "5")
    monkeypatch.setenv("AZURE_TTS_KEY", "secret-azure-tts")
    app = create_app()
    app.state.provider_runtime = {
        "tts_requests_last_minute": 420,
        "translation_requests_last_minute": 0,
        "provider_429_count": 2,
        "last_rate_limit_error": "429 for key secret-azure-tts",
    }

    with TestClient(app) as client:
        response = client.get("/api/providers/status?live=true")

    payload_text = response.text
    azure_tts = response.json()["tts"]["azure"]
    assert azure_tts["runtime"]["provider_429_count"] == 2
    assert azure_tts["runtime"]["last_rate_limit_error"] == "429 for key [redacted]"
    assert azure_tts["recommendation"] == "over_limit"
    assert "secret-azure-tts" not in payload_text


def test_config_check_reports_provider_near_limit_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'quota-config-check.db').as_posix()}")
    monkeypatch.setenv("AZURE_STT_MAX_CONCURRENT_SESSIONS", "5")
    app = create_app()
    app.state.session_manager.sessions = {f"lesson_{index}": FakeSession() for index in range(4)}

    with TestClient(app) as client:
        response = client.get("/api/system/config-check")

    payload = response.json()
    assert "PROVIDER_QUOTA_NEAR_LIMIT:stt.azure" in payload["warnings"]
    assert payload["checks"]["provider_quotas"]["stt"]["azure"]["recommendation"] == "near_limit"


class FakeSession:
    async def stop(self):
        return None
