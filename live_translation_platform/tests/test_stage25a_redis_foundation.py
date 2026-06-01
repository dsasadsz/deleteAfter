from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class FakeRedis:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_redis_disabled_keeps_app_in_memory_and_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-disabled.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert app.state.redis is None
    assert payload["status"] == "ready"
    assert payload["redis"] == {
        "enabled": False,
        "required": False,
        "connected": False,
        "url_configured": True,
        "latency_ms": None,
        "error": None,
    }


def test_redis_enabled_reports_connected_with_fake_client(tmp_path, monkeypatch):
    fake = FakeRedis()

    async def fake_create(settings):
        return fake

    async def fake_ping(client, timeout):
        assert client is fake
        assert timeout == 1
        return 12.5

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-enabled.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    monkeypatch.setattr("app.infra.redis.ping_redis", fake_ping)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert app.state.redis is fake
    assert payload["status"] == "ready"
    assert payload["redis"]["enabled"] is True
    assert payload["redis"]["connected"] is True
    assert payload["redis"]["latency_ms"] == 12.5
    assert payload["redis"]["error"] is None
    assert fake.closed is True


def test_redis_enabled_ping_failure_reports_sanitized_error(tmp_path, monkeypatch):
    async def fake_create(settings):
        return FakeRedis()

    async def fake_ping(client, timeout):
        raise RuntimeError("authentication failed for redis://:secret-pass@localhost:6379/0")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-failure.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://:secret-pass@localhost:6379/0")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    monkeypatch.setattr("app.infra.redis.ping_redis", fake_ping)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "ready"
    assert payload["redis"]["connected"] is False
    assert "secret-pass" not in response.text
    assert "redis://:secret-pass@localhost:6379/0" not in response.text
    assert payload["redis"]["error"]


def test_redis_create_failure_does_not_crash_optional_dev_app(tmp_path, monkeypatch):
    async def fake_create(settings):
        raise RuntimeError("cannot create redis://:startup-secret@localhost:6379/0")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-create-failure.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://:startup-secret@localhost:6379/0")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["redis"]["connected"] is False
    assert "startup-secret" not in response.text


def test_production_required_redis_failure_makes_readiness_and_config_check_error(tmp_path, monkeypatch):
    async def fake_create(settings):
        return FakeRedis()

    async def fake_ping(client, timeout):
        raise TimeoutError("redis timeout")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-prod-required.db').as_posix()}")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://example.test")
    monkeypatch.setenv("TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("ENABLE_OPENAPI_DOCS", "false")
    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "false")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("WEBSOCKET_AUTH_REQUIRED_IN_PRODUCTION", "false")
    monkeypatch.setenv("ZOOM_WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION", "false")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_REQUIRED_IN_PRODUCTION", "true")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    monkeypatch.setattr("app.infra.redis.ping_redis", fake_ping)
    app = create_app()

    with TestClient(app) as client:
        ready = client.get("/api/health/ready")
        config_check = client.get("/api/system/config-check")

    assert ready.status_code == 200
    assert ready.json()["status"] == "not_ready"
    assert ready.json()["redis"]["required"] is True
    assert ready.json()["redis"]["connected"] is False
    assert config_check.status_code == 200
    assert config_check.json()["status"] == "error"
    assert config_check.json()["checks"]["redis"]["connected"] is False


def test_redis_url_password_is_not_exposed_in_config_check(tmp_path, monkeypatch):
    async def fake_create(settings):
        return FakeRedis()

    async def fake_ping(client, timeout):
        raise RuntimeError("bad password redis://:very-secret@redis:6379/0")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-redaction.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://:very-secret@redis:6379/0")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    monkeypatch.setattr("app.infra.redis.ping_redis", fake_ping)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/system/config-check")

    assert response.status_code == 200
    assert "very-secret" not in response.text
    assert "redis://:very-secret@redis:6379/0" not in response.text
    assert response.json()["checks"]["redis"]["url_configured"] is True


def test_build_redis_key_applies_prefix():
    from app.infra.redis import build_redis_key

    settings = Settings(redis_prefix="live_translation")

    assert build_redis_key(settings, "test", "key") == "live_translation:test:key"
