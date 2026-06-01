from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import health as health_api
from app.config import Settings
from app.main import create_app
from app.production import production_config_check


def test_sqlite_dev_readiness_passes_and_reports_database_type(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'dev.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "development")

    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "ready"
    assert payload["database_status"] == "ok"
    assert payload["database"]["type"] == "sqlite"
    assert payload["database"]["production_ready"] is True
    assert payload["database_type"] == "sqlite"


def test_production_sqlite_fails_when_postgres_is_required(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'prod.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("POSTGRES_REQUIRED_IN_PRODUCTION", "true")
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

    assert ready.status_code == 200
    assert ready.json()["status"] == "not_ready"
    assert ready.json()["database_type"] == "sqlite"
    assert config.json()["status"] == "error"
    assert "DATABASE_POSTGRESQL_REQUIRED" in config.json()["missing"]


def test_postgres_url_detected_without_opening_network_connection():
    settings = Settings(database_url="postgresql+psycopg://app_user:secret-pass@db.example:5432/live_translation")

    payload = production_config_check(settings)

    assert payload["checks"]["database"]["type"] == "postgresql"
    assert payload["checks"]["database"]["production_ready"] is True


@pytest.mark.asyncio
async def test_health_ready_does_not_expose_database_password(monkeypatch):
    secret_url = "postgresql+psycopg://app_user:secret-pass@db.example:5432/live_translation"
    settings = Settings(database_url=secret_url)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                database=SimpleNamespace(engine=FakeEngine("postgresql")),
                redis_status=None,
            )
        )
    )

    async def fake_redis_status(_request):
        return SimpleNamespace(required=False, connected=False, to_dict=lambda: {"enabled": False, "required": False, "connected": False})

    monkeypatch.setattr(health_api, "_redis_status", fake_redis_status)
    monkeypatch.setattr(health_api, "_provider_summary", lambda _request: {})

    payload = await health_api.ready(request)

    assert payload["database_type"] == "postgresql"
    assert "secret-pass" not in str(payload)
    assert secret_url not in str(payload)


class FakeEngine:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)

    def connect(self):
        return FakeConnection()


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _statement):
        return None
