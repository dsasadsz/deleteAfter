import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.production import production_config_check, sanitize_for_log


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _production_env(monkeypatch, tmp_path, **overrides):
    values = {
        "APP_ENV": "production",
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'stage27a.db').as_posix()}",
        "POSTGRES_REQUIRED_IN_PRODUCTION": "false",
        "SQLITE_ALLOWED_IN_PRODUCTION": "true",
        "PUBLIC_BASE_URL": "https://python-service.example.com",
        "PUBLIC_WS_BASE_URL": "wss://python-service.example.com",
        "CORS_ALLOWED_ORIGINS": "https://csharp.example.com",
        "TRUSTED_HOSTS": "testserver,python-service.example.com",
        "ENABLE_OPENAPI_DOCS": "true",
        "DOCS_ENABLED_IN_PRODUCTION": "false",
        "ENABLE_DEBUG_ENDPOINTS": "true",
        "DEBUG_ENDPOINTS_ALLOWED_IN_PRODUCTION": "false",
        "WEBSOCKET_AUTH_REQUIRED_IN_PRODUCTION": "false",
        "ZOOM_WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION": "false",
        "LOG_FORMAT": "json",
        "SECURITY_HEADERS_ENABLED": "true",
        "MAX_REQUEST_BODY_BYTES": "1048576",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_security_headers_are_present_in_production_like_config(tmp_path, monkeypatch):
    _production_env(monkeypatch, tmp_path)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/live")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] in {"no-referrer", "strict-origin-when-cross-origin"}
    assert response.headers["x-frame-options"] in {"DENY", "SAMEORIGIN"}
    assert "microphone=" in response.headers["permissions-policy"]
    assert "strict-transport-security" in response.headers
    docs_response = client.get("/docs")
    assert docs_response.status_code == 404


def test_wildcard_cors_is_not_allowed_in_production_without_explicit_opt_in(tmp_path, monkeypatch):
    _production_env(monkeypatch, tmp_path, CORS_ALLOWED_ORIGINS="*", ALLOW_WILDCARD_CORS_IN_PRODUCTION="false")
    app = create_app()

    with TestClient(app) as client:
        response = client.options(
            "/api/health/live",
            headers={
                "Origin": "https://unknown.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.headers.get("access-control-allow-origin") != "*"
    payload = production_config_check(app.state.settings)
    assert "CORS_ALLOWED_ORIGINS" in payload["missing"]


def test_trusted_host_config_is_enforced_and_documented(tmp_path, monkeypatch):
    _production_env(monkeypatch, tmp_path, TRUSTED_HOSTS="python-service.example.com")
    app = create_app()

    with TestClient(app) as client:
        rejected = client.get("/api/health/live", headers={"host": "evil.example.com"})
        accepted = client.get("/api/health/live", headers={"host": "python-service.example.com"})

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert "trusted hosts" in _read("docs/security.md").lower()


def test_request_body_size_limit_rejects_oversized_body(tmp_path, monkeypatch):
    _production_env(monkeypatch, tmp_path, MAX_REQUEST_BODY_BYTES="8")
    app = create_app()

    with TestClient(app) as client:
        response = client.post("/api/health/live", content=b"x" * 32)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"


def test_token_like_values_are_redacted_from_log_safe_helpers():
    payload = sanitize_for_log(
        {
            "authorization": "Bearer real-token-value",
            "audio_url": "https://cdn.example.test/audio?id=1&token=signed-audio-token",
            "database_url": "postgresql://user:database-secret@example.test/app",
            "message": "Cookie: session=secret-cookie; Authorization: Bearer another-token",
            "nested": {"integration_key": "integration-secret", "safe": "ok"},
        }
    )
    encoded = json.dumps(payload)

    for secret in (
        "real-token-value",
        "signed-audio-token",
        "database-secret",
        "secret-cookie",
        "another-token",
        "integration-secret",
    ):
        assert secret not in encoded
    assert "token=<redacted>" in encoded
    assert payload["nested"]["safe"] == "ok"


def test_docs_security_exists_and_covers_required_topics():
    security = _read("docs/security.md")
    for phrase in (
        "CORS",
        "trusted hosts",
        "TLS/WSS",
        "tokens",
        "rate limits",
        "secret management",
        "logging redaction",
        "WebSocket token rules",
    ):
        assert phrase in security


def test_readme_and_production_docs_link_to_security_docs():
    assert "docs/security.md" in _read("README.md")
    assert "docs/security.md" in _read("docs/production.md")


def test_settings_support_stage27a_security_fields():
    settings = Settings(
        app_env="production",
        cors_allowed_origins="https://csharp.example.com",
        trusted_hosts="python-service.example.com",
    )

    assert settings.security_headers_active is True
    assert settings.allowed_origin_list == ["https://csharp.example.com"]
    assert settings.docs_enabled is False
    assert settings.debug_endpoints_allowed is False
