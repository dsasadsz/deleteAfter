import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.security.scopes import TTS_PLAY
from app.security.tokens import create_access_token
from app.tts.base import TTSResult


class CountingTTSProvider:
    name = "mock"
    calls = 0

    def status(self) -> dict:
        return {
            "ready": True,
            "status": "ready",
            "missing": [],
            "voices": {
                "kk": [_voice("mock-kk-1"), _voice("mock-kk-2")],
                "ru": [_voice("mock-ru-1")],
            },
            "default_voice_by_language": {"kk": "mock-kk-1", "ru": "mock-ru-1"},
        }

    async def synthesize(self, text, language, voice=None, audio_format=None, metadata=None, voice_gender=None):
        type(self).calls += 1
        await asyncio.sleep(0)
        selected_voice = voice or self.status()["default_voice_by_language"][language]
        return TTSResult(
            audio_bytes=f"audio:{language}:{selected_voice}:{text}".encode("utf-8"),
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


def test_same_caption_language_voice_calls_provider_once(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-shared-once.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        body = {"text": "Same caption", "language": "kk", "voice": "mock-kk-1", "caption_id": "cap-1"}
        first = client.post(url, json=body)
        second = client.post(url, json=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.headers["x-tts-cache"] == "miss"
    assert second.headers["x-tts-cache"] == "hit"
    assert first.headers["x-tts-cache-key"]
    assert first.headers["x-tts-cache-key"] == second.headers["x-tts-cache-key"]
    assert first.headers["x-tts-cached"] == "false"
    assert second.headers["x-tts-cached"] == "true"
    assert CountingTTSProvider.calls == 1


def test_different_voice_creates_different_shared_cache_key(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-shared-voice.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        first = client.post(url, json={"text": "Same caption", "language": "kk", "voice": "mock-kk-1", "caption_id": "cap-1"})
        second = client.post(url, json={"text": "Same caption", "language": "kk", "voice": "mock-kk-2", "caption_id": "cap-1"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.headers["x-tts-cached"] == "false"
    assert second.headers["x-tts-cached"] == "false"
    assert CountingTTSProvider.calls == 2


def test_different_language_creates_different_shared_cache_key(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-shared-language.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        first = client.post(url, json={"text": "Same caption", "language": "kk", "caption_id": "cap-1"})
        second = client.post(url, json={"text": "Same caption", "language": "ru", "caption_id": "cap-1"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.headers["x-tts-cached"] == "false"
    assert second.headers["x-tts-cached"] == "false"
    assert CountingTTSProvider.calls == 2


def test_url_return_mode_returns_audio_url_and_cached_metadata(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-url-mode.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        first = client.post(url, json={"text": "URL caption", "language": "kk", "caption_id": "cap-url", "return_mode": "url"})
        second = client.post(url, json={"text": "URL caption", "language": "kk", "caption_id": "cap-url", "return_mode": "url"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.headers["content-type"].startswith("application/json")
    assert first.json()["audio_url"].startswith(f"/api/lessons/{lesson['lesson_id']}/tts/audio/")
    assert "token=" in first.json()["audio_url"]
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert first.json()["provider"] == "mock"
    assert first.json()["voice"] == "mock-kk-1"
    assert first.json()["language"] == "kk"
    assert first.json()["caption_id"] == "cap-url"
    assert first.json()["audio_mime_type"] == "audio/wav"
    assert "cache_key" not in first.json()
    assert first.json()["expires_at"]
    assert CountingTTSProvider.calls == 1


def test_audio_url_returns_cached_audio_bytes(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-audio-url.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        synthesize = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"text": "Playable URL", "language": "kk", "caption_id": "cap-play", "return_mode": "url"},
        )
        audio = client.get(synthesize.json()["audio_url"])

    assert synthesize.status_code == 200, synthesize.text
    assert audio.status_code == 200, audio.text
    assert audio.content == b"audio:kk:mock-kk-1:Playable URL"
    assert audio.headers["content-type"].startswith("audio/wav")
    assert audio.headers["x-tts-cache"] == "hit"


def test_audio_url_rejects_invalid_token(tmp_path, monkeypatch):
    app = _app(
        tmp_path,
        monkeypatch,
        "tts-invalid-audio-token.db",
        auth_required=True,
    )

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _tts_token(lesson["lesson_id"])
        synthesize = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize?token={token}",
            json={"text": "Protected URL", "language": "kk", "caption_id": "cap-protected", "return_mode": "url"},
        )
        bad_url = synthesize.json()["audio_url"].split("?", 1)[0] + "?token=not-a-valid-token"
        audio = client.get(bad_url)

    assert synthesize.status_code == 200, synthesize.text
    assert audio.status_code == 401


def test_audio_url_rejects_token_without_tts_play(tmp_path, monkeypatch):
    app = _app(
        tmp_path,
        monkeypatch,
        "tts-audio-missing-scope.db",
        auth_required=True,
    )

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _tts_token(lesson["lesson_id"])
        read_only_token = _token_with_scopes(lesson["lesson_id"], ["captions:read"])
        synthesize = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize?token={token}",
            json={"text": "Protected scope URL", "language": "kk", "caption_id": "cap-scope", "return_mode": "url"},
        )
        audio_path = synthesize.json()["audio_url"].split("?", 1)[0]
        audio = client.get(f"{audio_path}?token={read_only_token}")

    assert synthesize.status_code == 200, synthesize.text
    assert audio.status_code == 403


def test_expired_shared_cache_misses(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-expired.db", ttl_seconds=0)

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        body = {"text": "Expires fast", "language": "kk", "caption_id": "cap-expire"}
        first = client.post(url, json=body)
        second = client.post(url, json=body)

    assert first.headers["x-tts-cached"] == "false"
    assert second.headers["x-tts-cached"] == "false"
    assert CountingTTSProvider.calls == 2


def test_unavailable_translation_text_is_not_synthesized_or_cached(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-unavailable.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"text": "Translation unavailable for kk", "language": "kk", "caption_id": "cap-unavailable"},
        )

    assert response.status_code == 400
    assert "unavailable" in response.json()["detail"].lower()
    assert CountingTTSProvider.calls == 0
    assert len(app.state.tts_shared_cache._items) == 0


def test_tts_rate_limit_still_applies_to_cached_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_RATE_LIMIT_PER_MINUTE", "1")
    app = _app(tmp_path, monkeypatch, "tts-rate-cache.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        body = {"text": "Rate limited cache", "language": "kk", "caption_id": "cap-rate"}
        first = client.post(url, json=body)
        second = client.post(url, json=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "TTS_RATE_LIMITED"
    assert CountingTTSProvider.calls == 1


def test_v1_tts_synthesize_supports_url_mode(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-v1-url.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _tts_token(lesson["lesson_id"])
        response = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/synthesize?token={token}",
            json={"text": "Integration URL", "language": "kk", "caption_id": "cap-v1", "return_mode": "url"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["audio_url"].startswith(f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/audio/")
    assert response.json()["language"] == "kk"


def test_v1_audio_url_accepts_signed_tts_play_token_without_integration_key(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-v1-audio-token.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _tts_token(lesson["lesson_id"])
        synthesize = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/synthesize?token={token}",
            json={"text": "Browser safe URL", "language": "kk", "caption_id": "cap-browser", "return_mode": "url"},
        )
        audio = client.get(synthesize.json()["audio_url"])

    assert synthesize.status_code == 200, synthesize.text
    assert audio.status_code == 200, audio.text
    assert audio.content == b"audio:kk:mock-kk-1:Browser safe URL"
    assert audio.headers["content-type"].startswith("audio/wav")
    assert audio.headers["x-tts-cache"] == "hit"


def test_v1_audio_url_rejects_missing_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-v1-audio-missing-token.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _tts_token(lesson["lesson_id"])
        synthesize = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/synthesize?token={token}",
            json={"text": "Missing token URL", "language": "kk", "caption_id": "cap-missing-token", "return_mode": "url"},
        )
        audio_path = synthesize.json()["audio_url"].split("?", 1)[0]
        audio = client.get(audio_path)

    assert synthesize.status_code == 200, synthesize.text
    assert audio.status_code == 401


def test_v1_audio_url_rejects_wrong_lesson_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-v1-audio-wrong-lesson.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        other_lesson = _create_lesson(client)
        token = _tts_token(lesson["lesson_id"])
        wrong_lesson_token = _tts_token(other_lesson["lesson_id"])
        synthesize = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/synthesize?token={token}",
            json={"text": "Wrong lesson URL", "language": "kk", "caption_id": "cap-wrong-lesson", "return_mode": "url"},
        )
        audio_path = synthesize.json()["audio_url"].split("?", 1)[0]
        audio = client.get(f"{audio_path}?token={wrong_lesson_token}")

    assert synthesize.status_code == 200, synthesize.text
    assert audio.status_code == 403


def test_v1_audio_url_rejects_token_without_tts_play(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-v1-audio-missing-scope.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _tts_token(lesson["lesson_id"])
        read_only_token = _token_with_scopes(lesson["lesson_id"], ["captions:read"])
        synthesize = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/synthesize?token={token}",
            json={"text": "Missing scope URL", "language": "kk", "caption_id": "cap-missing-scope", "return_mode": "url"},
        )
        audio_path = synthesize.json()["audio_url"].split("?", 1)[0]
        audio = client.get(f"{audio_path}?token={read_only_token}")

    assert synthesize.status_code == 200, synthesize.text
    assert audio.status_code == 403


def test_v1_audio_url_rejects_expired_token(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-v1-audio-expired-token.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _tts_token(lesson["lesson_id"])
        expired_token = _tts_token(lesson["lesson_id"], ttl_seconds=0)
        synthesize = client.post(
            f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/synthesize?token={token}",
            json={"text": "Expired token URL", "language": "kk", "caption_id": "cap-expired-token", "return_mode": "url"},
        )
        audio_path = synthesize.json()["audio_url"].split("?", 1)[0]
        audio = client.get(f"{audio_path}?token={expired_token}")

    assert synthesize.status_code == 200, synthesize.text
    assert audio.status_code == 401


def test_runtime_metrics_include_tts_shared_cache_counters(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-cache-metrics.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        body = {"text": "Metric caption", "language": "kk", "caption_id": "cap-metrics"}
        first = client.post(url, json=body)
        second = client.post(url, json=body)
        metrics = client.get("/api/metrics/runtime")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert metrics.status_code == 200, metrics.text
    payload = metrics.json()
    assert payload["tts_cache_misses_total"] == 1
    assert payload["tts_cache_hits_total"] == 1
    assert payload["tts_cache_items"] == 1
    assert payload["tts_provider_calls_total"] == 1
    assert payload["tts_provider_calls_saved_total"] == 1


@pytest.mark.asyncio
async def test_shared_cache_coalesces_concurrent_requests():
    from app.tts.shared_cache import MemoryTTSSharedCache, build_tts_shared_cache_key

    CountingTTSProvider.calls = 0
    cache = MemoryTTSSharedCache(max_items=100, ttl_seconds=3600)
    provider = CountingTTSProvider()
    key = build_tts_shared_cache_key(
        lesson_id="lesson-1",
        caption_id="cap-1",
        language="kk",
        provider="mock",
        voice="mock-kk-1",
        text="Concurrent caption",
    )

    async def synthesize_once():
        return await cache.get_or_synthesize(
            key,
            lambda: provider.synthesize("Concurrent caption", "kk", "mock-kk-1", "audio/wav"),
        )

    results = await asyncio.gather(*[synthesize_once() for _ in range(100)])

    assert CountingTTSProvider.calls == 1
    assert results[0].cached is False
    assert all(result.result.audio_bytes == results[0].result.audio_bytes for result in results)
    assert sum(1 for result in results if result.cached) == 99


def test_shared_cache_key_contains_caption_and_hash_without_raw_text():
    from app.tts.shared_cache import build_tts_shared_cache_key

    key = build_tts_shared_cache_key(
        lesson_id="lesson-1",
        caption_id="cap-1",
        language="kk",
        provider="mock",
        voice="mock-kk-1",
        text="Secret text",
    )

    assert "lesson-1" in key
    assert "cap-1" in key
    assert "mock-kk-1" in key
    assert "Secret text" not in key


def test_student_tts_js_uses_url_mode_when_status_enables_audio_url():
    script = open("app/web/static/student_tts.js", encoding="utf-8").read()

    assert "audio_url_enabled" in script
    assert 'return_mode: "url"' in script
    assert "audio url received" in script
    assert "retrying direct audio" in script
    assert "X-Integration-Key" not in script
    assert "integration_key" not in script


def test_integration_spec_documents_tts_audio_url_mode():
    from app.integration.spec import integration_spec

    spec = integration_spec()

    assert "/api/v1/integration/lessons/{lesson_id}/tts/audio/{audio_id}" in spec["http_endpoints"]
    assert spec["tts"]["return_modes"] == ["audio", "url"]
    assert spec["tts"]["audio_scope"] == "tts:play"
    assert "does not require X-Integration-Key" in spec["tts"]["audio_auth"]
    assert spec["auth"]["integration_key_usage"] == "backend_only"
    assert "audio_url" in spec["tts"]["returns"]


def _voice(voice_id: str) -> dict:
    return {
        "id": voice_id,
        "name": voice_id,
        "display_name": voice_id,
        "gender": "unknown",
        "provider": "mock",
        "language": "ru" if "-ru-" in voice_id else "kk",
    }


def _app(tmp_path, monkeypatch, db_name: str, ttl_seconds: int = 3600, auth_required: bool = False):
    CountingTTSProvider.calls = 0
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("TTS_SHARED_CACHE_ENABLED", "true")
    monkeypatch.setenv("TTS_SHARED_CACHE_MAX_ITEMS", "100")
    monkeypatch.setenv("TTS_SHARED_CACHE_TTL_SECONDS", str(ttl_seconds))
    monkeypatch.setenv("TTS_AUDIO_URL_ENABLED", "true")
    monkeypatch.setenv("TTS_AUDIO_URL_TOKEN_REQUIRED", "true")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage25e-test-secret")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    if auth_required:
        monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "true")
        monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "false")
    monkeypatch.setattr("app.api.tts._create_provider", lambda settings, provider_name=None: CountingTTSProvider())
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post("/api/lessons", json={"title": "Stage 25E", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock"})
    assert response.status_code == 201, response.text
    return response.json()


def _tts_token(lesson_id: str, ttl_seconds: int = 3600) -> str:
    return _token_with_scopes(lesson_id, [TTS_PLAY], ttl_seconds=ttl_seconds)


def _token_with_scopes(lesson_id: str, scopes: list[str], ttl_seconds: int = 3600) -> str:
    return create_access_token(
        {
            "sub": "student-1",
            "role": "student",
            "lesson_id": lesson_id,
            "scopes": scopes,
        },
        ttl_seconds=ttl_seconds,
    )
