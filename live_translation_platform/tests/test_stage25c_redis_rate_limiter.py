import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security.rate_limit import InMemoryRateLimiter, RedisRateLimiter, check_rate_limit, rate_limit_key


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
async def test_redis_limiter_increments_and_blocks_over_limit(monkeypatch):
    redis = FakeRedis()
    settings = Settings(redis_prefix="live_translation", redis_rate_limit_fail_closed=False)
    monkeypatch.setattr("app.security.rate_limit.time.time", lambda: 125.0)
    limiter = RedisRateLimiter(redis, settings, window_seconds=60)
    key = rate_limit_key("tts", "lesson_a", "token:student_a")

    first = await limiter.check(key, limit=2)
    second = await limiter.check(key, limit=2)
    third = await limiter.check(key, limit=2)

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert third.retry_after_seconds == 55
    redis_key = "live_translation:rate:tts:lesson_a:token:student_a:120"
    assert redis.counts[redis_key] == 3
    assert redis.expirations[redis_key] == 60


@pytest.mark.asyncio
async def test_redis_limiter_shares_counts_for_same_key_and_window(monkeypatch):
    redis = FakeRedis()
    settings = Settings(redis_prefix="live_translation")
    monkeypatch.setattr("app.security.rate_limit.time.time", lambda: 10.0)
    limiter_a = RedisRateLimiter(redis, settings, window_seconds=60)
    limiter_b = RedisRateLimiter(redis, settings, window_seconds=60)
    key = rate_limit_key("question_text", "lesson_a", "student:one")

    assert (await limiter_a.check(key, limit=1)).allowed is True
    assert (await limiter_b.check(key, limit=1)).allowed is False


@pytest.mark.asyncio
async def test_redis_limiter_isolates_different_lessons(monkeypatch):
    redis = FakeRedis()
    settings = Settings(redis_prefix="live_translation")
    monkeypatch.setattr("app.security.rate_limit.time.time", lambda: 10.0)
    limiter = RedisRateLimiter(redis, settings, window_seconds=60)

    assert (await limiter.check(rate_limit_key("tts", "lesson_a", "token:student"), limit=1)).allowed is True
    assert (await limiter.check(rate_limit_key("tts", "lesson_b", "token:student"), limit=1)).allowed is True


@pytest.mark.asyncio
async def test_redis_limiter_isolates_different_subjects(monkeypatch):
    redis = FakeRedis()
    settings = Settings(redis_prefix="live_translation")
    monkeypatch.setattr("app.security.rate_limit.time.time", lambda: 10.0)
    limiter = RedisRateLimiter(redis, settings, window_seconds=60)

    assert (await limiter.check(rate_limit_key("tts", "lesson_a", "token:student_a"), limit=1)).allowed is True
    assert (await limiter.check(rate_limit_key("tts", "lesson_a", "token:student_b"), limit=1)).allowed is True


def test_redis_disabled_uses_memory_limiter(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'memory.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "false")
    monkeypatch.setenv("REDIS_RATE_LIMIT_ENABLED", "true")

    app = create_app()

    assert isinstance(app.state.rate_limiter, InMemoryRateLimiter)


def test_app_uses_redis_limiter_when_redis_and_rate_limit_are_enabled(tmp_path, monkeypatch):
    fake = FakeRedis()

    async def fake_create(settings):
        return fake

    async def fake_ping(client, timeout):
        return 1.0

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'redis-limiter.db').as_posix()}")
    monkeypatch.setenv("REDIS_ENABLED", "true")
    monkeypatch.setenv("REDIS_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr("app.infra.redis.create_redis_client", fake_create)
    monkeypatch.setattr("app.infra.redis.ping_redis", fake_ping)

    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/health/ready")

    assert response.status_code == 200
    assert isinstance(app.state.rate_limiter, RedisRateLimiter)


@pytest.mark.asyncio
async def test_redis_error_fails_open_by_default_without_exposing_secret():
    settings = Settings(
        redis_url="redis://:secret-pass@localhost:6379/0",
        redis_rate_limit_fail_closed=False,
    )
    limiter = RedisRateLimiter(FakeRedis(fail=True), settings, window_seconds=60)

    result = await limiter.check(rate_limit_key("tts", "lesson_a", "token:student"), limit=1)

    assert result.allowed is True
    assert "secret-pass" not in (result.backend_error or "")


@pytest.mark.asyncio
async def test_redis_error_fail_closed_rejects_as_backend_unavailable():
    settings = Settings(
        redis_url="redis://:secret-pass@localhost:6379/0",
        redis_rate_limit_fail_closed=True,
    )
    limiter = RedisRateLimiter(FakeRedis(fail=True), settings, window_seconds=60)

    result = await limiter.check(rate_limit_key("tts", "lesson_a", "token:student"), limit=1)

    assert result.allowed is False
    assert result.backend_unavailable is True
    assert result.retry_after_seconds == 60
    assert "secret-pass" not in (result.backend_error or "")


@pytest.mark.asyncio
async def test_check_rate_limit_supports_memory_and_redis_limiters(monkeypatch):
    memory = InMemoryRateLimiter(window_seconds=60)
    redis = FakeRedis()
    settings = Settings(redis_prefix="live_translation")
    monkeypatch.setattr("app.security.rate_limit.time.time", lambda: 10.0)
    redis_limiter = RedisRateLimiter(redis, settings, window_seconds=60)

    assert (await check_rate_limit(memory, "scope:lesson:lesson_a:subject", 1)).allowed is True
    assert (await check_rate_limit(redis_limiter, "scope:lesson:lesson_a:subject", 1)).allowed is True
