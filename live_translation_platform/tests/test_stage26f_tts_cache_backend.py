import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.monitoring.metrics import runtime_metrics_snapshot
from app.tts.base import TTSResult
from app.tts.shared_cache import DiskTTSSharedCache, MemoryTTSSharedCache, build_tts_shared_cache_key


class CountingTTSProvider:
    name = "mock"
    calls = 0

    def status(self) -> dict:
        return {
            "ready": True,
            "status": "ready",
            "missing": [],
            "voices": {"kk": [_voice("mock-kk-1")]},
            "default_voice_by_language": {"kk": "mock-kk-1"},
        }

    async def synthesize(self, text, language, voice=None, audio_format=None, metadata=None, voice_gender=None):
        type(self).calls += 1
        await asyncio.sleep(0)
        selected_voice = voice or "mock-kk-1"
        return TTSResult(
            audio_bytes=f"disk-audio:{language}:{selected_voice}:{text}".encode("utf-8"),
            content_type="audio/wav",
            language=language,
            voice=selected_voice,
            provider=self.name,
            duration_ms=100,
            text_chars=len(text),
            cached=False,
            latency_ms=3,
            metadata=metadata or {},
        )


def test_memory_shared_cache_remains_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'memory-default.db').as_posix()}")

    app = create_app()

    assert isinstance(app.state.tts_shared_cache, MemoryTTSSharedCache)


@pytest.mark.asyncio
async def test_disk_backend_cache_hit_survives_new_cache_instance(tmp_path):
    cache_dir = tmp_path / "tts-cache"
    key = _cache_key("Persistent caption")
    first_cache = DiskTTSSharedCache(cache_dir=cache_dir, max_items=100, ttl_seconds=3600, max_bytes=1024 * 1024)

    first = await first_cache.get_or_synthesize(
        key,
        lambda: _result("Persistent caption"),
        lesson_id="lesson-1",
    )
    second_cache = DiskTTSSharedCache(cache_dir=cache_dir, max_items=100, ttl_seconds=3600, max_bytes=1024 * 1024)
    second = await second_cache.get_or_synthesize(
        key,
        lambda: _result("should-not-run"),
        lesson_id="lesson-1",
    )

    assert first.cached is False
    assert second.cached is True
    assert second.result.audio_bytes == b"disk-audio:Persistent caption"
    assert second.audio_id == first.audio_id


@pytest.mark.asyncio
async def test_disk_backend_coalesces_concurrent_same_key_requests_in_process(tmp_path):
    calls = 0
    cache = DiskTTSSharedCache(cache_dir=tmp_path / "concurrent-cache", max_items=100, ttl_seconds=3600, max_bytes=1024 * 1024)
    key = _cache_key("Concurrent disk caption")

    async def synthesize_once():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return await _result("Concurrent disk caption")

    results = await asyncio.gather(
        *[
            cache.get_or_synthesize(key, synthesize_once, lesson_id="lesson-1")
            for _ in range(50)
        ]
    )

    assert calls == 1
    assert results[0].cached is False
    assert sum(1 for result in results if result.cached) == 49


def test_disk_backend_audio_url_returns_bytes(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "disk-audio-url.db", cache_dir=tmp_path / "disk-cache")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        synthesize = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"text": "Disk URL", "language": "kk", "caption_id": "cap-disk", "return_mode": "url"},
        )
        audio = client.get(synthesize.json()["audio_url"])

    assert synthesize.status_code == 200, synthesize.text
    assert audio.status_code == 200, audio.text
    assert audio.content == b"disk-audio:kk:mock-kk-1:Disk URL"
    assert audio.headers["x-tts-cache"] == "hit"
    assert CountingTTSProvider.calls == 1


@pytest.mark.asyncio
async def test_expired_disk_entry_is_unavailable(tmp_path):
    cache = DiskTTSSharedCache(cache_dir=tmp_path / "expired-cache", max_items=100, ttl_seconds=0, max_bytes=1024 * 1024)
    key = _cache_key("Expired caption")

    stored = await cache.get_or_synthesize(key, lambda: _result("Expired caption"), lesson_id="lesson-1")

    assert cache.get_audio(stored.audio_id, "lesson-1") is None
    assert cache.get(key) is None


@pytest.mark.asyncio
async def test_cleanup_removes_old_files_when_disk_max_bytes_exceeded(tmp_path):
    cache = DiskTTSSharedCache(cache_dir=tmp_path / "cleanup-cache", max_items=100, ttl_seconds=3600, max_bytes=80)

    await cache.get_or_synthesize(_cache_key("First large caption"), lambda: _result("A" * 70), lesson_id="lesson-1")
    await cache.get_or_synthesize(_cache_key("Second large caption"), lambda: _result("B" * 70), lesson_id="lesson-1")

    stats = cache.stats()
    assert stats["tts_cache_disk_bytes"] <= 80
    assert stats["tts_cache_evictions_total"] >= 1
    assert len(list((tmp_path / "cleanup-cache").glob("*.audio"))) <= 1


@pytest.mark.asyncio
async def test_disk_backend_does_not_put_raw_text_in_filenames(tmp_path):
    raw_text = "Secret lesson text"
    cache_dir = tmp_path / "safe-filenames"
    cache = DiskTTSSharedCache(cache_dir=cache_dir, max_items=100, ttl_seconds=3600, max_bytes=1024 * 1024)

    await cache.get_or_synthesize(_cache_key(raw_text), lambda: _result(raw_text), lesson_id="lesson-1")

    filenames = [path.name for path in cache_dir.iterdir()]
    assert filenames
    assert all("Secret" not in name and "lesson" not in name and "text" not in name for name in filenames)


def test_runtime_metrics_include_disk_backend_fields(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "disk-metrics.db", cache_dir=tmp_path / "metrics-cache")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"text": "Metric disk", "language": "kk", "caption_id": "cap-metrics"},
        )
        payload = client.get("/api/metrics/runtime").json()

    assert payload["tts_cache_backend"] == "disk"
    assert payload["tts_cache_disk_bytes"] > 0
    assert payload["tts_cache_evictions_total"] == 0


def _app(tmp_path, monkeypatch, db_name: str, *, cache_dir: Path):
    CountingTTSProvider.calls = 0
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("TTS_SHARED_CACHE_ENABLED", "true")
    monkeypatch.setenv("TTS_SHARED_CACHE_BACKEND", "disk")
    monkeypatch.setenv("TTS_SHARED_CACHE_DIR", cache_dir.as_posix())
    monkeypatch.setenv("TTS_SHARED_CACHE_MAX_ITEMS", "100")
    monkeypatch.setenv("TTS_SHARED_CACHE_TTL_SECONDS", "3600")
    monkeypatch.setenv("TTS_SHARED_CACHE_DISK_MAX_BYTES", str(1024 * 1024))
    monkeypatch.setenv("TTS_AUDIO_URL_ENABLED", "true")
    monkeypatch.setenv("TTS_AUDIO_URL_TOKEN_REQUIRED", "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    monkeypatch.setattr("app.api.tts._create_provider", lambda settings, provider_name=None: CountingTTSProvider())
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post("/api/lessons", json={"title": "Stage 26F", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock"})
    assert response.status_code == 201, response.text
    return response.json()


def _cache_key(text: str) -> str:
    return build_tts_shared_cache_key(
        lesson_id="lesson-1",
        caption_id="cap-1",
        language="kk",
        provider="mock",
        voice="mock-kk-1",
        text=text,
    )


async def _result(text: str) -> TTSResult:
    return TTSResult(
        audio_bytes=f"disk-audio:{text}".encode("utf-8"),
        content_type="audio/wav",
        language="kk",
        voice="mock-kk-1",
        provider="mock",
        duration_ms=100,
        text_chars=len(text),
        cached=False,
        latency_ms=3,
        metadata={"lesson_id": "lesson-1", "caption_id": "cap-1"},
    )


def _voice(voice_id: str) -> dict:
    return {
        "id": voice_id,
        "name": voice_id,
        "display_name": voice_id,
        "gender": "unknown",
        "provider": "mock",
        "language": "kk",
    }
