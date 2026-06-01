from fastapi.testclient import TestClient

from app.config import Settings
from app.infra.redis import build_redis_key, redact_redis_url, sanitize_redis_error
from app.main import create_app


class FakeRedis:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_redis_disabled_app_starts_and_readiness_stays_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-disabled.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "false")

    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "ready"
    assert app.state.redis is None
    assert payload["redis"] == {
        "enabled": False,
        "required": False,
        "connected": False,
        "url_configured": True,
        "latency_ms": None,
        "error": None,
    }


def test_redis_enabled_fake_ping_ok_reports_connected_and_closes_client(tmp_path, monkeypatch):
    fake = FakeRedis()

    async def fake_create(settings):
        return fake

    async def fake_ping(client, timeout):
        assert client is fake
        assert timeout == 1
        return 7.25

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-ok.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    monkeypatch.setattr("app.infra.redis.ping_redis", fake_ping)

    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "ready"
    assert payload["redis"]["enabled"] is True
    assert payload["redis"]["required"] is False
    assert payload["redis"]["connected"] is True
    assert payload["redis"]["latency_ms"] == 7.25
    assert payload["redis"]["error"] is None
    assert fake.closed is True


def test_redis_enabled_fake_ping_failure_is_sanitized_and_optional_in_dev(tmp_path, monkeypatch):
    async def fake_create(settings):
        return FakeRedis()

    async def fake_ping(client, timeout):
        raise RuntimeError("AUTH failed for redis://:secret-pass@localhost:6379/0")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-fail.db').as_posix()}")
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
    assert payload["redis"]["error"]
    assert "secret-pass" not in response.text
    assert "redis://:secret-pass@localhost:6379/0" not in response.text


def test_production_required_redis_ping_failure_makes_readiness_fail(tmp_path, monkeypatch):
    async def fake_create(settings):
        return FakeRedis()

    async def fake_ping(client, timeout):
        raise TimeoutError("redis timeout")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-required.db').as_posix()}")
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
    assert config_check.json()["status"] == "error"
    assert "REDIS_AVAILABLE" in config_check.json()["missing"]


def test_build_redis_key_adds_prefix_and_skips_empty_parts():
    settings = Settings(redis_prefix="live_translation")

    assert build_redis_key(settings, "rate", "", ":lesson_1:", "student_2") == "live_translation:rate:lesson_1:student_2"


def test_redis_url_password_is_never_exposed_by_redaction_helpers():
    settings = Settings(redis_url="redis://user:very-secret@redis:6379/0")
    error = RuntimeError("cannot connect to redis://user:very-secret@redis:6379/0 with password very-secret")

    assert redact_redis_url(settings.redis_url) == "redis://user:[redacted]@redis:6379/0"
    assert "very-secret" not in sanitize_redis_error(error, settings)


def test_compose_exposes_all_redis_foundation_env_switches():
    compose = open("docker-compose.prod.yml", encoding="utf-8").read()

    assert "REDIS_ENABLED: ${REDIS_ENABLED:-false}" in compose
    assert "REDIS_URL: ${COMPOSE_REDIS_URL:-redis://redis:6379/0}" in compose
    assert "REDIS_PREFIX: ${REDIS_PREFIX:-live_translation}" in compose
    assert "REDIS_REQUIRED_IN_PRODUCTION: ${REDIS_REQUIRED_IN_PRODUCTION:-false}" in compose
    assert "REDIS_CONNECT_TIMEOUT_SECONDS: ${REDIS_CONNECT_TIMEOUT_SECONDS:-2}" in compose
    assert "REDIS_HEALTH_TIMEOUT_SECONDS: ${REDIS_HEALTH_TIMEOUT_SECONDS:-1}" in compose
    assert "REDIS_RATE_LIMIT_ENABLED: ${REDIS_RATE_LIMIT_ENABLED:-false}" in compose
    assert "REDIS_PUBSUB_ENABLED: ${REDIS_PUBSUB_ENABLED:-false}" in compose
    assert "REDIS_TTS_CACHE_ENABLED: ${REDIS_TTS_CACHE_ENABLED:-false}" in compose
    assert "redis:\n        condition: service_healthy" in compose
