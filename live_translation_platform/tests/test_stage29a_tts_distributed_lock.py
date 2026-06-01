import asyncio
import re
from pathlib import Path

import pytest

from app.config import Settings
from app.monitoring.metrics import RuntimeMetrics, runtime_metrics_snapshot
from app.tts.base import TTSConfigurationError, TTSResult
from app.tts import shared_cache
from app.tts.shared_cache import (
    DiskTTSSharedCache,
    build_tts_shared_cache_key,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeRedisLock:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.locks: dict[str, str] = {}
        self.set_keys: list[str] = []

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None):
        if self.fail:
            raise RuntimeError("redis lock failed for redis://:secret@localhost:6379/0")
        self.set_keys.append(key)
        if nx and key in self.locks:
            return False
        self.locks[key] = value
        return True

    async def get(self, key: str):
        if self.fail:
            raise RuntimeError("redis lock failed for redis://:secret@localhost:6379/0")
        return self.locks.get(key)

    async def delete(self, key: str):
        if self.fail:
            raise RuntimeError("redis lock failed for redis://:secret@localhost:6379/0")
        self.locks.pop(key, None)
        return 1


@pytest.mark.asyncio
async def test_same_key_concurrent_calls_coalesce_across_fake_workers(tmp_path):
    redis = FakeRedisLock()
    metrics = RuntimeMetrics()
    settings = _settings()
    key = _cache_key("Secret lesson text")
    calls = 0
    started = asyncio.Event()

    async def synthesize_once():
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(0.05)
        return _result("Secret lesson text")

    cache_a = DiskTTSSharedCache(tmp_path / "tts-cache", ttl_seconds=3600)
    cache_b = DiskTTSSharedCache(tmp_path / "tts-cache", ttl_seconds=3600)
    first_task = asyncio.create_task(
        shared_cache.get_or_synthesize_with_distributed_lock(cache_a, key, synthesize_once, settings=settings, redis_client=redis, runtime_metrics=metrics, lesson_id="lesson-1")
    )
    await started.wait()
    second = await shared_cache.get_or_synthesize_with_distributed_lock(cache_b, key, synthesize_once, settings=settings, redis_client=redis, runtime_metrics=metrics, lesson_id="lesson-1")
    first = await first_task

    assert calls == 1
    assert first.cached is False
    assert second.cached is True
    assert second.result.audio_bytes == first.result.audio_bytes
    assert metrics.tts_distributed_lock_acquired_total == 1
    assert metrics.tts_distributed_lock_waited_total == 1


@pytest.mark.asyncio
async def test_lock_timeout_fail_open_synthesizes_anyway(tmp_path):
    redis = FakeRedisLock()
    metrics = RuntimeMetrics()
    settings = _settings(tts_cache_distributed_lock_wait_timeout_seconds=0.01, tts_cache_distributed_lock_fail_closed=False)
    key = _cache_key("Timeout caption")
    await redis.set(shared_cache.build_tts_distributed_lock_key(settings, key), "other-worker", nx=True, ex=30)
    calls = 0

    async def synthesize_once():
        nonlocal calls
        calls += 1
        return _result("Timeout caption")

    result = await shared_cache.get_or_synthesize_with_distributed_lock(
        DiskTTSSharedCache(tmp_path / "timeout-open", ttl_seconds=3600),
        key,
        synthesize_once,
        settings=settings,
        redis_client=redis,
        runtime_metrics=metrics,
        lesson_id="lesson-1",
    )

    assert result.cached is False
    assert calls == 1
    assert metrics.tts_distributed_lock_waited_total == 1
    assert metrics.tts_distributed_lock_timeout_total == 1


@pytest.mark.asyncio
async def test_lock_timeout_fail_closed_returns_configuration_error(tmp_path):
    redis = FakeRedisLock()
    metrics = RuntimeMetrics()
    settings = _settings(tts_cache_distributed_lock_wait_timeout_seconds=0.01, tts_cache_distributed_lock_fail_closed=True)
    key = _cache_key("Closed timeout caption")
    await redis.set(shared_cache.build_tts_distributed_lock_key(settings, key), "other-worker", nx=True, ex=30)

    with pytest.raises(TTSConfigurationError):
        await shared_cache.get_or_synthesize_with_distributed_lock(
            DiskTTSSharedCache(tmp_path / "timeout-closed", ttl_seconds=3600),
            key,
            lambda: _async_result("should-not-run"),
            settings=settings,
            redis_client=redis,
            runtime_metrics=metrics,
            lesson_id="lesson-1",
        )

    assert metrics.tts_distributed_lock_timeout_total == 1


@pytest.mark.asyncio
async def test_redis_error_fail_open_and_fail_closed(tmp_path):
    key = _cache_key("Redis error caption")
    open_metrics = RuntimeMetrics()
    open_result = await shared_cache.get_or_synthesize_with_distributed_lock(
        DiskTTSSharedCache(tmp_path / "error-open", ttl_seconds=3600),
        key,
        lambda: _async_result("Redis error caption"),
        settings=_settings(tts_cache_distributed_lock_fail_closed=False),
        redis_client=FakeRedisLock(fail=True),
        runtime_metrics=open_metrics,
        lesson_id="lesson-1",
    )
    assert open_result.cached is False
    assert open_metrics.tts_distributed_lock_errors_total == 1

    closed_metrics = RuntimeMetrics()
    with pytest.raises(TTSConfigurationError) as exc:
        await shared_cache.get_or_synthesize_with_distributed_lock(
            DiskTTSSharedCache(tmp_path / "error-closed", ttl_seconds=3600),
            key,
            lambda: _async_result("should-not-run"),
            settings=_settings(tts_cache_distributed_lock_fail_closed=True),
            redis_client=FakeRedisLock(fail=True),
            runtime_metrics=closed_metrics,
            lesson_id="lesson-1",
        )

    assert "secret" not in str(exc.value)
    assert closed_metrics.tts_distributed_lock_errors_total == 1


@pytest.mark.asyncio
async def test_lock_key_hashes_cache_key_and_excludes_raw_text(tmp_path):
    redis = FakeRedisLock()
    settings = _settings(redis_prefix="prefix")
    key = _cache_key("Secret lesson text")

    await shared_cache.get_or_synthesize_with_distributed_lock(
        DiskTTSSharedCache(tmp_path / "safe-key", ttl_seconds=3600),
        key,
        lambda: _async_result("Secret lesson text"),
        settings=settings,
        redis_client=redis,
        lesson_id="lesson-1",
    )

    assert redis.set_keys
    assert all("Secret" not in redis_key and "lesson text" not in redis_key for redis_key in redis.set_keys)
    assert re.fullmatch(r"prefix:tts:lock:[0-9a-f]{64}", redis.set_keys[0])


def test_runtime_metrics_snapshot_includes_tts_distributed_lock_fields():
    metrics = RuntimeMetrics()
    metrics.record_tts_distributed_lock_acquired()
    metrics.record_tts_distributed_lock_waited()
    metrics.record_tts_distributed_lock_timeout()
    metrics.record_tts_distributed_lock_error()

    payload = runtime_metrics_snapshot(_metrics_app(metrics))

    assert payload["tts_distributed_lock_acquired_total"] == 1
    assert payload["tts_distributed_lock_waited_total"] == 1
    assert payload["tts_distributed_lock_timeout_total"] == 1
    assert payload["tts_distributed_lock_errors_total"] == 1


def test_docs_describe_optional_tts_distributed_lock():
    production = (ROOT / "docs" / "production.md").read_text(encoding="utf-8")
    load_testing = (ROOT / "docs" / "load-testing.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    combined = "\n".join([production, load_testing, architecture])

    assert "TTS_CACHE_DISTRIBUTED_LOCK_ENABLED" in combined
    assert "tts_distributed_lock_acquired_total" in combined
    assert "no raw text" in combined
    assert "future Redis distributed lock" not in production
    assert "future Redis distributed lock" not in load_testing
    assert "future Redis distributed lock" not in architecture


def _settings(**overrides):
    values = {
        "redis_enabled": True,
        "redis_prefix": "live_translation",
        "tts_cache_distributed_lock_enabled": True,
        "tts_cache_distributed_lock_ttl_seconds": 5,
        "tts_cache_distributed_lock_wait_timeout_seconds": 0.5,
        "tts_cache_distributed_lock_poll_interval_seconds": 0.01,
        "tts_cache_distributed_lock_fail_closed": False,
    }
    values.update(overrides)
    return Settings(**values)


def _cache_key(text: str) -> str:
    return build_tts_shared_cache_key(
        lesson_id="lesson-1",
        caption_id="cap-1",
        language="kk",
        provider="mock",
        voice="mock-kk-1",
        text=text,
    )


def _result(text: str) -> TTSResult:
    return TTSResult(
        audio_bytes=f"audio:{text}".encode("utf-8"),
        content_type="audio/wav",
        language="kk",
        voice="mock-kk-1",
        provider="mock",
        duration_ms=100,
        text_chars=len(text),
        cached=False,
        latency_ms=3,
        metadata={"lesson_id": "lesson-1"},
    )


async def _async_result(text: str) -> TTSResult:
    await asyncio.sleep(0)
    return _result(text)


def _metrics_app(metrics: RuntimeMetrics):
    class State:
        pass

    state = State()
    state.runtime_metrics = metrics
    state.settings = Settings(redis_enabled=True)
    state.redis_status = type("RedisStatus", (), {"to_dict": lambda self: {"enabled": True, "connected": True}})()
    state.rate_limiter = None
    state.session_manager = type("SessionManager", (), {"sessions": {}})()
    state.caption_hub = type("Hub", (), {"_caption_clients": {}, "_debug_clients": {}})()
    state.question_hub = type("Hub", (), {"_clients": {}})()
    state.rtms_manager = None
    state.browser_audio_manager = None
    state.provider_runtime = {}
    state.tts_shared_cache = None
    return type("App", (), {"state": state})()
