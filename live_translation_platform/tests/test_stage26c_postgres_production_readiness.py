import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.db.database import database_type_from_url, normalize_database_url_for_sync_engine
from app.main import create_app
from app.production import database_config_summary, production_config_check


def test_dev_sqlite_database_summary_is_allowed():
    settings = Settings(app_env="development", database_url="sqlite:///./dev.db")

    summary = database_config_summary(settings)

    assert summary["type"] == "sqlite"
    assert summary["url_configured"] is True
    assert summary["production_ready"] is True
    assert summary["error"] is None


def test_production_sqlite_fails_readiness_with_strict_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'prod.db').as_posix()}")
    monkeypatch.setenv("POSTGRES_REQUIRED_IN_PRODUCTION", "true")
    monkeypatch.setenv("SQLITE_ALLOWED_IN_PRODUCTION", "false")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.test")
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("ENABLE_OPENAPI_DOCS", "false")
    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "false")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("WEBSOCKET_AUTH_REQUIRED_IN_PRODUCTION", "false")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION", "false")
    app = create_app()

    with TestClient(app) as client:
        ready = client.get("/api/health/ready")
        config = client.get("/api/system/config-check")

    database = ready.json()["database"]
    assert ready.status_code == 200
    assert ready.json()["status"] == "not_ready"
    assert database["type"] == "sqlite"
    assert database["production_ready"] is False
    assert "PostgreSQL" in database["error"]
    assert config.json()["status"] == "error"
    assert "DATABASE_POSTGRESQL_REQUIRED" in config.json()["missing"]


def test_production_postgres_config_check_passes_without_network_connection():
    settings = _production_settings("postgresql+psycopg://app_user:secret-pass@db.example:5432/live_translation")

    payload = production_config_check(settings)

    assert payload["status"] == "ok"
    assert payload["checks"]["database"]["type"] == "postgresql"
    assert payload["checks"]["database"]["production_ready"] is True


def test_postgres_url_password_is_not_exposed_in_database_summary():
    secret_url = "postgresql+psycopg://app_user:secret-pass@db.example:5432/live_translation"
    settings = _production_settings(secret_url)

    summary = database_config_summary(settings)

    encoded = json.dumps(summary)
    assert "secret-pass" not in encoded
    assert "db.example" not in encoded
    assert secret_url not in encoded


def test_invalid_database_url_reports_unknown_database():
    settings = Settings(app_env="production", database_url="://bad-url")

    summary = database_config_summary(settings)

    assert database_type_from_url("://bad-url") == "unknown"
    assert summary["type"] == "unknown"
    assert summary["production_ready"] is False
    assert "invalid" in summary["error"].lower()


def test_database_type_accepts_postgresql_url_variants():
    assert database_type_from_url("postgresql://user:pw@db/live_translation") == "postgresql"
    assert database_type_from_url("postgresql+psycopg://user:pw@db/live_translation") == "postgresql"
    assert database_type_from_url("postgresql+asyncpg://user:pw@db/live_translation") == "postgresql"


def test_plain_postgresql_url_is_normalized_to_sync_psycopg_driver_without_network_connection():
    url = normalize_database_url_for_sync_engine("postgresql://user:pw@db.example:5432/live_translation")

    assert url.drivername == "postgresql+psycopg"


def test_health_ready_reports_structured_database_object(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'ready.db').as_posix()}")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "ready"
    assert payload["database"]["type"] == "sqlite"
    assert payload["database"]["url_configured"] is True
    assert payload["database"]["production_ready"] is True
    assert payload["database"]["error"] is None
    assert payload["database_type"] == "sqlite"


def test_health_ready_database_connection_failure_is_structured_and_sanitized(tmp_path, monkeypatch):
    secret_url = "postgresql+psycopg://live_translation:secret-pass@bad-hostname:5432/live_translation"
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'ready-db-failure.db').as_posix()}")
    monkeypatch.setenv("POSTGRES_REQUIRED_IN_PRODUCTION", "false")
    monkeypatch.setenv("SQLITE_ALLOWED_IN_PRODUCTION", "true")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.test")
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("ENABLE_OPENAPI_DOCS", "false")
    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "false")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("WEBSOCKET_AUTH_REQUIRED_IN_PRODUCTION", "false")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION", "false")
    app = create_app()
    app.state.settings.database_url = secret_url
    app.state.database.database_type = "postgresql"
    app.state.database.engine = _FailingEngine()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "not_ready"
    assert payload["database_status"] == "error"
    assert payload["database"]["type"] == "postgresql"
    assert payload["database"]["production_ready"] is False
    assert payload["database"]["error"] == "Database connectivity check failed."
    assert "secret-pass" not in response.text
    assert "bad-hostname" not in response.text
    assert secret_url not in response.text


class _FailingEngine:
    dialect = type("Dialect", (), {"name": "postgresql"})()

    def connect(self):
        raise OSError("could not resolve host bad-hostname with password secret-pass")


def _production_settings(database_url: str, **overrides) -> Settings:
    values = {
        "app_env": "production",
        "database_url": database_url,
        "public_base_url": "https://example.test",
        "allowed_origins": "https://example.test",
        "trusted_hosts": "testserver",
        "enable_openapi_docs": False,
        "enable_debug_endpoints": False,
        "log_format": "json",
        "websocket_auth_required_in_production": False,
        "zoom_webhook_signature_required_in_production": False,
        "postgres_required_in_production": True,
        "sqlite_allowed_in_production": False,
    }
    values.update(overrides)
    return Settings(**values)
