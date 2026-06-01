import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import _create_rate_limiter, create_app
from app.monitoring.metrics import RuntimeMetrics, runtime_metrics_snapshot
from app.security.rate_limit import InMemoryRateLimiter, RedisRateLimiter, rate_limit_key


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.counts: dict[str, int] = {}
        self.expirations: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        if self.fail:
            raise RuntimeError("redis auth failed for redis://:secret-pass@localhost:6379/0")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> bool:
        if self.fail:
            raise RuntimeError("redis auth failed for redis://:secret-pass@localhost:6379/0")
        self.expirations[key] = seconds
        return True


@pytest.mark.asyncio
async def test_redis_limiter_allows_under_limit_and_blocks_over_limit(monkeypatch):
    redis = FakeRedis()
    metrics = RuntimeMetrics()
    settings = Settings(redis_prefix="live_translation")
    monkeypatch.setattr("app.security.rate_limit.time.time", lambda: 125.0)
    limiter = RedisRateLimiter(redis, settings, window_seconds=60, runtime_metrics=metrics)

    key = rate_limit_key("tts", "lesson_a", "token:student_a")
    first = await limiter.check(key, limit=2)
    second = await limiter.check(key, limit=2)
    third = await limiter.check(key, limit=2)

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.retry_after_seconds == 55
    assert redis.counts["live_translation:rate:tts:lesson_a:token:student_a:120"] == 3
    assert metrics.rate_limit_checks_total == 3
    assert metrics.rate_limit_blocked_total == 1


@pytest.mark.asyncio
async def test_redis_limiter_shares_same_key_across_fake_workers(monkeypatch):
    redis = FakeRedis()
    settings = Settings(redis_prefix="live_translation")
    monkeypatch.setattr("app.security.rate_limit.time.time", lambda: 10.0)
    worker_a = RedisRateLimiter(redis, settings, window_seconds=60)
    worker_b = RedisRateLimiter(redis, settings, window_seconds=60)
    key = rate_limit_key("question_text", "lesson_a", "student:one")

    assert (await worker_a.check(key, limit=1)).allowed is True
    assert (await worker_b.check(key, limit=1)).allowed is False


@pytest.mark.asyncio
async def test_redis_limiter_isolates_different_lessons_and_subjects(monkeypatch):
    redis = FakeRedis()
    settings = Settings(redis_prefix="live_translation")
    monkeypatch.setattr("app.security.rate_limit.time.time", lambda: 10.0)
    limiter = RedisRateLimiter(redis, settings, window_seconds=60)

    assert (await limiter.check(rate_limit_key("tts", "lesson_a", "token:student_a"), limit=1)).allowed is True
    assert (await limiter.check(rate_limit_key("tts", "lesson_b", "token:student_a"), limit=1)).allowed is True
    assert (await limiter.check(rate_limit_key("tts", "lesson_a", "token:student_b"), limit=1)).allowed is True


def test_redis_disabled_or_missing_client_uses_memory_limiter():
    disabled = Settings(redis_enabled=False, redis_rate_limit_enabled=True)
    missing_client = Settings(redis_enabled=True, redis_rate_limit_enabled=True)

    assert isinstance(_create_rate_limiter(disabled, None, RuntimeMetrics()), InMemoryRateLimiter)
    assert isinstance(_create_rate_limiter(missing_client, None, RuntimeMetrics()), InMemoryRateLimiter)


@pytest.mark.asyncio
async def test_redis_error_fail_open_allows_request_and_increments_error_metric():
    metrics = RuntimeMetrics()
    settings = Settings(redis_url="redis://:secret-pass@localhost:6379/0", redis_rate_limit_fail_closed=False)
    limiter = RedisRateLimiter(FakeRedis(fail=True), settings, window_seconds=60, runtime_metrics=metrics)

    result = await limiter.check(rate_limit_key("tts", "lesson_a", "token:student"), limit=1)

    assert result.allowed is True
    assert metrics.rate_limit_checks_total == 1
    assert metrics.redis_rate_limit_errors_total == 1
    assert "secret-pass" not in (result.backend_error or "")


@pytest.mark.asyncio
async def test_redis_error_fail_closed_blocks_and_increments_error_metric():
    metrics = RuntimeMetrics()
    settings = Settings(redis_url="redis://:secret-pass@localhost:6379/0", redis_rate_limit_fail_closed=True)
    limiter = RedisRateLimiter(FakeRedis(fail=True), settings, window_seconds=60, runtime_metrics=metrics)

    result = await limiter.check(rate_limit_key("tts", "lesson_a", "token:student"), limit=1)

    assert result.allowed is False
    assert result.backend_unavailable is True
    assert metrics.rate_limit_checks_total == 1
    assert metrics.rate_limit_blocked_total == 1
    assert metrics.redis_rate_limit_errors_total == 1
    assert "secret-pass" not in (result.backend_error or "")


def test_runtime_metrics_snapshot_includes_rate_limit_fields():
    metrics = RuntimeMetrics()
    metrics.record_rate_limit_check()
    metrics.record_rate_limit_blocked()
    metrics.record_redis_rate_limit_error()
    app = _metrics_app(metrics, Settings(redis_enabled=True, redis_rate_limit_enabled=True), RedisRateLimiter(FakeRedis(), Settings()))

    payload = runtime_metrics_snapshot(app)

    assert payload["rate_limit_checks_total"] == 1
    assert payload["rate_limit_blocked_total"] == 1
    assert payload["redis_rate_limit_errors_total"] == 1
    assert payload["redis_rate_limit_enabled"] is True


def test_readiness_reports_redis_rate_limit_degraded_without_fail_closed(tmp_path, monkeypatch):
    async def fake_create(settings):
        return FakeRedis()

    async def fake_ping(client, timeout):
        raise TimeoutError("redis unavailable")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-rl-open.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("REDIS_RATE_LIMIT_FAIL_CLOSED", "false")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    monkeypatch.setattr("app.infra.redis.ping_redis", fake_ping)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "ready"
    assert payload["redis_rate_limit_enabled"] is True
    assert payload["redis_rate_limit"]["connected"] is False
    assert "REDIS_RATE_LIMIT_DEGRADED" in payload["config_warnings"]


def test_readiness_fails_redis_rate_limit_when_fail_closed(tmp_path, monkeypatch):
    async def fake_create(settings):
        return FakeRedis()

    async def fake_ping(client, timeout):
        raise TimeoutError("redis unavailable")

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-rl-closed.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("REDIS_RATE_LIMIT_FAIL_CLOSED", "true")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    monkeypatch.setattr("app.infra.redis.ping_redis", fake_ping)
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "not_ready"
    assert payload["redis_rate_limit_enabled"] is True
    assert payload["redis_rate_limit"]["connected"] is False
    assert "REDIS_RATE_LIMIT_AVAILABLE" in payload["config_missing"]


def _metrics_app(metrics: RuntimeMetrics, settings: Settings, rate_limiter):
    class State:
        pass

    state = State()
    state.runtime_metrics = metrics
    state.settings = settings
    state.redis_status = type("RedisStatus", (), {"to_dict": lambda self: {"enabled": True, "connected": True}})()
    state.rate_limiter = rate_limiter
    state.session_manager = type("SessionManager", (), {"sessions": {}})()
    state.caption_hub = type("Hub", (), {"_caption_clients": {}, "_debug_clients": {}})()
    state.question_hub = type("Hub", (), {"_clients": {}})()
    state.rtms_manager = None
    state.browser_audio_manager = None
    state.provider_runtime = {}
    state.tts_shared_cache = None
    return type("App", (), {"state": state})()
