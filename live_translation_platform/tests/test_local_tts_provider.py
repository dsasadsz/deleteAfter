import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.tts.base import TTSConfigurationError
from app.tts.cache import TTSCache, synthesize_with_cache
from app.tts.factory import create_tts_provider
from app.tts.local_engines.base import LocalTTSSynthesisResult
from app.tts.local_tts import LocalTTSProvider


def test_tts_factory_selects_local_provider():
    provider = create_tts_provider(
        "local",
        enabled=True,
        piper_enabled=True,
        piper_bin_path="piper",
        piper_voices={"kk": "voices/kk.onnx"},
    )

    assert isinstance(provider, LocalTTSProvider)
    assert provider.name == "local"


@pytest.mark.asyncio
async def test_local_tts_missing_piper_config_reports_readiness_error():
    provider = LocalTTSProvider(
        enabled=True,
        default_engine="piper",
        piper_enabled=True,
        piper_bin_path="",
        piper_voices={"kk": ""},
    )

    status = provider.status()

    assert status["ready"] is False
    assert status["status"] == "not_configured"
    assert "PIPER_BIN_PATH" in status["missing"]
    assert "PIPER_VOICE_KK" in status["missing"]
    with pytest.raises(TTSConfigurationError, match="PIPER_BIN_PATH"):
        await provider.synthesize("Сәлем", "kk")


def test_piper_missing_binary_reports_not_configured():
    from app.tts.local_engines.piper import PiperTTSEngine

    engine = PiperTTSEngine(enabled=True, bin_path="", voices={"kk": "C:/voices/kk.onnx"})

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "not_configured"
    assert status["missing"] == ["PIPER_BIN_PATH"]
    assert "C:/voices" not in str(status)


def test_piper_missing_selected_voice_reports_not_configured():
    from app.tts.local_engines.piper import PiperTTSEngine

    engine = PiperTTSEngine(enabled=True, bin_path="piper", voices={"kk": ""})

    status = engine.status_for_language("kk")

    assert status["ready"] is False
    assert status["status"] == "not_configured"
    assert "PIPER_VOICE_KK" in status["missing"]


@pytest.mark.asyncio
async def test_piper_fake_synthesize_returns_wav_bytes_and_mime_type():
    from app.tts.local_engines.piper import PiperTTSEngine

    runner = FakePiperRunner(audio_bytes=b"RIFFfake-wav")
    engine = PiperTTSEngine(
        enabled=True,
        bin_path="piper",
        voices={"kk": "C:/private/voices/kk.onnx"},
        timeout_seconds=0.5,
        runner=runner,
    )

    result = await engine.synthesize("Сәлем", "kk", "piper-kk", "wav")

    assert result.audio_bytes == b"RIFFfake-wav"
    assert result.content_type == "audio/wav"
    assert runner.calls == [("Сәлем", "kk", "C:/private/voices/kk.onnx", "wav")]
    status = engine.status()
    assert status["request_count"] == 1
    assert status["last_error"] is None
    assert "C:/private" not in str(status)


@pytest.mark.asyncio
async def test_piper_timeout_returns_clean_provider_error():
    from app.tts.local_engines.piper import PiperTTSEngine

    engine = PiperTTSEngine(
        enabled=True,
        bin_path="piper",
        voices={"kk": "C:/private/voices/kk.onnx"},
        timeout_seconds=0.01,
        runner=FakePiperRunner(audio_bytes=b"RIFFslow", delay_seconds=0.05),
    )

    with pytest.raises(TTSConfigurationError, match="timeout"):
        await engine.synthesize("slow", "kk", "piper-kk", "wav")

    status = engine.status()
    assert status["timeout_count"] == 1
    assert "C:/private" not in (status["last_error"] or "")


def test_silero_disabled_reports_disabled_status():
    from app.tts.local_engines.silero import SileroTTSEngine

    engine = SileroTTSEngine(enabled=False, model_path="", device="cpu")

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "disabled"
    assert status["missing"] == []
    assert status["loaded"] is False


def test_kazakh_tts2_disabled_reports_disabled_status():
    from app.tts.local_engines.kazakh_tts2 import KazakhTTS2Engine

    engine = KazakhTTS2Engine(enabled=False)

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "disabled"
    assert status["missing"] == []
    assert status["loaded"] is False


def test_kazakh_tts2_missing_model_or_server_reports_not_configured():
    from app.tts.local_engines.kazakh_tts2 import KazakhTTS2Engine

    engine = KazakhTTS2Engine(enabled=True, model_path="", vocoder_path="", tokenizer_path="", server_url="")

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "not_configured"
    assert "KAZAKH_TTS2_MODEL_PATH" in status["missing"]
    assert "KAZAKH_TTS2_VOCODER_PATH" in status["missing"]
    assert "KAZAKH_TTS2_TOKENIZER_PATH" in status["missing"]
    assert "KAZAKH_TTS2_SERVER_URL" in status["missing"]


def test_kazakh_tts2_configured_lazy_status_does_not_load_model():
    backend = FakeKazakhTTS2Backend(audio_bytes=b"RIFFkazakh")
    from app.tts.local_engines.kazakh_tts2 import KazakhTTS2Engine

    engine = KazakhTTS2Engine(
        enabled=True,
        model_path="C:/private/kazakh/model",
        vocoder_path="C:/private/kazakh/vocoder",
        tokenizer_path="C:/private/kazakh/tokenizer",
        backend=backend,
    )

    status = engine.status()

    assert status["ready"] is True
    assert status["status"] == "configured"
    assert status["loaded"] is False
    assert backend.load_calls == 0
    assert "C:/private" not in str(status)


@pytest.mark.asyncio
async def test_kazakh_tts2_fake_synthesize_returns_wav_bytes_and_mime_type():
    backend = FakeKazakhTTS2Backend(audio_bytes=b"RIFFkazakh")
    from app.tts.local_engines.kazakh_tts2 import KazakhTTS2Engine

    engine = KazakhTTS2Engine(
        enabled=True,
        model_path="C:/private/kazakh/model",
        vocoder_path="C:/private/kazakh/vocoder",
        tokenizer_path="C:/private/kazakh/tokenizer",
        timeout_seconds=0.5,
        backend=backend,
    )

    result = await engine.synthesize("Сәлем, сынып", "kk", "kazakh_tts2-kk", "wav")

    assert result.audio_bytes == b"RIFFkazakh"
    assert result.content_type == "audio/wav"
    assert backend.load_calls == 1
    assert backend.calls == [("Сәлем, сынып", "kk", "kazakh_tts2-kk", "wav")]
    assert engine.status()["status"] == "loaded"


@pytest.mark.asyncio
async def test_kazakh_tts2_timeout_returns_clean_provider_error():
    backend = FakeKazakhTTS2Backend(audio_bytes=b"RIFFslow", delay_seconds=0.05)
    from app.tts.local_engines.kazakh_tts2 import KazakhTTS2Engine

    engine = KazakhTTS2Engine(
        enabled=True,
        model_path="C:/private/kazakh/model",
        vocoder_path="C:/private/kazakh/vocoder",
        tokenizer_path="C:/private/kazakh/tokenizer",
        timeout_seconds=0.01,
        backend=backend,
    )

    with pytest.raises(TTSConfigurationError, match="timeout"):
        await engine.synthesize("slow", "kk", "kazakh_tts2-kk", "wav")

    status = engine.status()
    assert status["timeout_count"] == 1
    assert "C:/private" not in (status["last_error"] or "")


@pytest.mark.asyncio
async def test_kazakh_tts2_inference_exception_is_sanitized():
    backend = FakeKazakhTTS2Backend(error=RuntimeError("failed at C:/private/kazakh/model with token=secret"))
    from app.tts.local_engines.kazakh_tts2 import KazakhTTS2Engine

    engine = KazakhTTS2Engine(
        enabled=True,
        model_path="C:/private/kazakh/model",
        vocoder_path="C:/private/kazakh/vocoder",
        tokenizer_path="C:/private/kazakh/tokenizer",
        backend=backend,
    )

    with pytest.raises(TTSConfigurationError) as exc_info:
        await engine.synthesize("boom", "kk", "kazakh_tts2-kk", "wav")

    assert "C:/private" not in str(exc_info.value)
    assert "secret" not in str(exc_info.value)
    assert "<redacted" in str(exc_info.value)


@pytest.mark.asyncio
async def test_silero_missing_model_reports_clean_not_configured_error():
    from app.tts.local_engines.silero import SileroTTSEngine

    engine = SileroTTSEngine(enabled=True, model_path="", device="cpu")

    status = engine.status()

    assert status["ready"] is False
    assert status["status"] == "not_configured"
    assert "SILERO_TTS_MODEL_PATH" in status["missing"]
    with pytest.raises(TTSConfigurationError, match="SILERO_TTS_MODEL_PATH"):
        await engine.synthesize("Привет", "ru", "silero-ru", "wav")


@pytest.mark.asyncio
async def test_local_tts_routes_languages_to_configured_engines():
    piper = FakeLocalTTSEngine("piper", {"kk", "uz", "zh-Hans"})
    silero = FakeLocalTTSEngine("silero", {"ru"})
    provider = LocalTTSProvider(
        enabled=True,
        default_engine="piper",
        ru_engine="silero",
        kk_engine="piper",
        uz_engine="piper",
        zh_engine="piper",
        engines={"piper": piper, "silero": silero},
    )

    kk = await provider.synthesize("Kazakh", "kk")
    uz = await provider.synthesize("Uzbek", "uz")
    zh = await provider.synthesize("Chinese", "zh-Hans")
    ru = await provider.synthesize("Russian", "ru")

    assert kk.metadata["engine"] == "piper"
    assert uz.metadata["engine"] == "piper"
    assert zh.metadata["engine"] == "piper"
    assert ru.metadata["engine"] == "silero"
    assert piper.calls == [("Kazakh", "kk"), ("Uzbek", "uz"), ("Chinese", "zh-Hans")]
    assert silero.calls == [("Russian", "ru")]


@pytest.mark.asyncio
async def test_local_tts_routes_kk_to_kazakh_tts2_when_configured():
    kazakh = FakeLocalTTSEngine("kazakh_tts2", {"kk"})
    piper = FakeLocalTTSEngine("piper", {"uz", "zh-Hans"})
    silero = FakeLocalTTSEngine("silero", {"ru"})
    provider = LocalTTSProvider(
        enabled=True,
        default_engine="piper",
        ru_engine="silero",
        kk_engine="kazakh_tts2",
        uz_engine="piper",
        zh_engine="piper",
        engines={"kazakh_tts2": kazakh, "piper": piper, "silero": silero},
    )

    kk = await provider.synthesize("Kazakh", "kk")
    ru = await provider.synthesize("Russian", "ru")

    assert kk.metadata["engine"] == "kazakh_tts2"
    assert ru.metadata["engine"] == "silero"
    assert kazakh.calls == [("Kazakh", "kk")]
    assert silero.calls == [("Russian", "ru")]


@pytest.mark.asyncio
async def test_local_tts_disabled_uzbek_engine_does_not_fallback_to_piper():
    piper = FakeLocalTTSEngine("piper", {"kk", "uz", "zh-Hans"})
    provider = LocalTTSProvider(
        enabled=True,
        default_engine="piper",
        uz_engine="disabled",
        engines={"piper": piper},
    )

    with pytest.raises(TTSConfigurationError, match="disabled for uz"):
        await provider.synthesize("Uzbek", "uz")

    assert piper.calls == []
    status = provider.status()
    assert status["selected_engine_by_language"]["uz"] == "disabled"
    assert status["language_status"]["uz"]["status"] == "disabled"


@pytest.mark.asyncio
async def test_local_tts_disabled_russian_engine_does_not_fallback_to_silero():
    silero = FakeLocalTTSEngine("silero", {"ru"})
    provider = LocalTTSProvider(
        enabled=True,
        default_engine="piper",
        ru_engine="disabled",
        engines={"silero": silero},
    )

    with pytest.raises(TTSConfigurationError, match="disabled for ru"):
        await provider.synthesize("Russian", "ru")

    assert silero.calls == []
    status = provider.status()
    assert status["selected_engine_by_language"]["ru"] == "disabled"
    assert status["language_status"]["ru"]["status"] == "disabled"


@pytest.mark.asyncio
async def test_local_tts_allowed_languages_block_uz_and_ru():
    piper = FakeLocalTTSEngine("piper", {"kk", "uz", "zh-Hans"})
    silero = FakeLocalTTSEngine("silero", {"ru"})
    provider = LocalTTSProvider(
        enabled=True,
        default_engine="piper",
        ru_engine="silero",
        kk_engine="piper",
        uz_engine="piper",
        zh_engine="piper",
        allowed_languages="kk,zh-Hans",
        engines={"piper": piper, "silero": silero},
    )

    kk = await provider.synthesize("Kazakh", "kk")
    zh = await provider.synthesize("Chinese", "zh-Hans")
    with pytest.raises(TTSConfigurationError, match="disabled for uz"):
        await provider.synthesize("Uzbek", "uz")
    with pytest.raises(TTSConfigurationError, match="disabled for ru"):
        await provider.synthesize("Russian", "ru")

    assert kk.metadata["engine"] == "piper"
    assert zh.metadata["engine"] == "piper"
    assert piper.calls == [("Kazakh", "kk"), ("Chinese", "zh-Hans")]
    assert silero.calls == []
    status = provider.status()
    assert status["language_status"]["uz"]["status"] == "disabled"
    assert status["language_status"]["ru"]["status"] == "disabled"


@pytest.mark.asyncio
async def test_local_tts_chinese_aliases_normalize_to_zh_hans():
    piper = FakeLocalTTSEngine("piper", {"zh-Hans"})
    provider = LocalTTSProvider(
        enabled=True,
        default_engine="piper",
        zh_engine="piper",
        allowed_languages="zh,zh-CN,zh-Hans",
        engines={"piper": piper},
    )

    result = await provider.synthesize("Chinese", "zh-CN")

    assert result.language == "zh-Hans"
    assert result.voice == "piper-zh"
    assert piper.calls == [("Chinese", "zh-Hans")]


@pytest.mark.asyncio
async def test_local_tts_cache_hit_does_not_call_engine_again():
    engine = FakeLocalTTSEngine("piper", {"kk"})
    provider = LocalTTSProvider(
        enabled=True,
        default_engine="piper",
        kk_engine="piper",
        engines={"piper": engine},
    )
    cache = TTSCache(max_items=10)

    first = await synthesize_with_cache(cache, provider, "Same caption", "kk", "piper-kk", "audio/wav")
    second = await synthesize_with_cache(cache, provider, "Same caption", "kk", "piper-kk", "audio/wav")

    assert first.cached is False
    assert second.cached is True
    assert len(engine.calls) == 1


def test_local_tts_direct_response_works_with_kazakh_tts2(tmp_path, monkeypatch):
    engine = FakeLocalTTSEngine("kazakh_tts2", {"kk"})
    provider = LocalTTSProvider(enabled=True, default_engine="piper", kk_engine="kazakh_tts2", engines={"kazakh_tts2": engine})
    monkeypatch.setattr(
        "app.api.tts._create_provider",
        lambda settings, provider_name=None: provider if (provider_name or settings.tts_provider) == "local" else create_tts_provider(provider_name or settings.tts_provider),
    )
    app = _local_tts_app(tmp_path, monkeypatch, "kazakh-tts2-direct.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"text": "Сәлем, сынып", "language": "kk", "caption_id": "cap-kazakh-direct"},
        )

    assert response.status_code == 200, response.text
    assert response.content.startswith(b"RIFF")
    assert response.headers["x-tts-provider"] == "local"
    assert response.headers["x-tts-voice"] == "kazakh_tts2-kk"
    assert engine.calls == [("Сәлем, сынып", "kk")]


def test_local_tts_url_mode_works_with_kazakh_tts2_and_cache_hit(tmp_path, monkeypatch):
    engine = FakeLocalTTSEngine("kazakh_tts2", {"kk"})
    provider = LocalTTSProvider(enabled=True, default_engine="piper", kk_engine="kazakh_tts2", engines={"kazakh_tts2": engine})
    monkeypatch.setattr(
        "app.api.tts._create_provider",
        lambda settings, provider_name=None: provider if (provider_name or settings.tts_provider) == "local" else create_tts_provider(provider_name or settings.tts_provider),
    )
    app = _local_tts_app(tmp_path, monkeypatch, "kazakh-tts2-url.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        body = {"text": "URL Kazakh caption", "language": "kk", "caption_id": "cap-kazakh-url", "return_mode": "url"}
        first = client.post(url, json=body)
        second = client.post(url, json=body)
        audio = client.get(first.json()["audio_url"])

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert first.json()["voice"] == "kazakh_tts2-kk"
    assert audio.status_code == 200, audio.text
    assert audio.content.startswith(b"RIFF")
    assert engine.calls == [("URL Kazakh caption", "kk")]


def test_local_tts_status_endpoint_reports_configured_provider(tmp_path, monkeypatch):
    app = _local_tts_app(tmp_path, monkeypatch, "local-tts-status.db")

    with TestClient(app) as client:
        response = client.get("/api/tts/status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider"] == "local"
    assert payload["active_provider"] == "local"
    assert payload["ready"] is True
    assert payload["providers"]["local"]["ready"] is True
    assert payload["providers"]["local"]["engines"]["piper"]["status"] == "ready"
    assert payload["providers"]["local"]["selected_engine_by_language"]["kk"] == "piper"
    assert payload["default_voice_by_language"]["kk"] == "piper-kk"
    assert payload["voices"]["kk"][0]["id"] == "piper-kk"


def test_local_tts_shared_cache_path_still_hits_on_repeated_caption(tmp_path, monkeypatch):
    engine = FakeLocalTTSEngine("piper", {"kk"})
    provider = LocalTTSProvider(enabled=True, default_engine="piper", kk_engine="piper", engines={"piper": engine})
    monkeypatch.setattr(
        "app.api.tts._create_provider",
        lambda settings, provider_name=None: provider if (provider_name or settings.tts_provider) == "local" else create_tts_provider(provider_name or settings.tts_provider),
    )
    app = _local_tts_app(tmp_path, monkeypatch, "local-tts-cache.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        body = {"text": "Сәлем, сынып", "language": "kk", "caption_id": "cap-local"}
        first = client.post(url, json=body)
        second = client.post(url, json=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.content.startswith(b"RIFF")
    assert first.headers["x-tts-provider"] == "local"
    assert first.headers["x-tts-cache"] == "miss"
    assert second.headers["x-tts-cache"] == "hit"
    assert second.headers["x-tts-cached"] == "true"
    assert len(engine.calls) == 1


def test_local_tts_url_mode_still_returns_cached_audio_url(tmp_path, monkeypatch):
    engine = FakeLocalTTSEngine("piper", {"kk"})
    provider = LocalTTSProvider(enabled=True, default_engine="piper", kk_engine="piper", engines={"piper": engine})
    monkeypatch.setattr(
        "app.api.tts._create_provider",
        lambda settings, provider_name=None: provider if (provider_name or settings.tts_provider) == "local" else create_tts_provider(provider_name or settings.tts_provider),
    )
    app = _local_tts_app(tmp_path, monkeypatch, "local-tts-url.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        body = {"text": "URL caption", "language": "kk", "caption_id": "cap-local-url", "return_mode": "url"}
        first = client.post(url, json=body)
        second = client.post(url, json=body)
        audio = client.get(first.json()["audio_url"])

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["audio_url"].startswith(f"/api/lessons/{lesson['lesson_id']}/tts/audio/")
    assert first.json()["cached"] is False
    assert second.json()["cached"] is True
    assert first.json()["audio_mime_type"] == "audio/wav"
    assert audio.status_code == 200, audio.text
    assert audio.content.startswith(b"RIFF")
    assert len(engine.calls) == 1


def test_v1_integration_tts_status_keeps_working_with_local_provider(tmp_path, monkeypatch):
    app = _local_tts_app(tmp_path, monkeypatch, "local-tts-v1-status.db")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.get(f"/api/v1/integration/lessons/{lesson['lesson_id']}/tts/status")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["provider"] == "local"
    assert payload["ready"] is True


def _local_tts_app(tmp_path, monkeypatch, db_name: str):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TTS_PROVIDER", "local")
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("LOCAL_TTS_ENABLED", "true")
    monkeypatch.setenv("LOCAL_TTS_DEFAULT_ENGINE", "piper")
    monkeypatch.setenv("LOCAL_TTS_KK_ENGINE", "kazakh_tts2")
    monkeypatch.setenv("PIPER_ENABLED", "true")
    monkeypatch.setenv("PIPER_BIN_PATH", "piper")
    monkeypatch.setenv("PIPER_VOICE_KK", "voices/kk.onnx")
    monkeypatch.setenv("PIPER_VOICE_RU", "voices/ru.onnx")
    monkeypatch.setenv("TTS_SHARED_CACHE_ENABLED", "true")
    monkeypatch.setenv("TTS_SHARED_CACHE_BACKEND", "memory")
    monkeypatch.setenv("TTS_AUDIO_URL_TOKEN_REQUIRED", "false")
    monkeypatch.setenv("INTEGRATION_AUTH_ENABLED", "false")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "local-tts-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post("/api/lessons", json={"title": "Local TTS", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock"})
    assert response.status_code == 201, response.text
    return response.json()


class FakePiperRunner:
    def __init__(self, audio_bytes: bytes, delay_seconds: float = 0.0) -> None:
        self.audio_bytes = audio_bytes
        self.delay_seconds = delay_seconds
        self.calls: list[tuple[str, str, str, str]] = []

    async def synthesize(self, *, text: str, language: str, voice_path: str, output_format: str) -> bytes:
        import asyncio

        self.calls.append((text, language, voice_path, output_format))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.audio_bytes


class FakeKazakhTTS2Backend:
    def __init__(self, audio_bytes: bytes = b"RIFFfake-kazakh", delay_seconds: float = 0.0, error: Exception | None = None) -> None:
        self.audio_bytes = audio_bytes
        self.delay_seconds = delay_seconds
        self.error = error
        self.loaded = False
        self.load_calls = 0
        self.calls: list[tuple[str, str, str | None, str | None]] = []

    async def load(self) -> None:
        self.load_calls += 1
        self.loaded = True

    async def synthesize(self, text: str, language: str, voice: str | None, output_format: str | None) -> LocalTTSSynthesisResult:
        import asyncio

        self.calls.append((text, language, voice, output_format))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error
        return LocalTTSSynthesisResult(self.audio_bytes, "audio/wav", 300)


class FakeLocalTTSEngine:
    def __init__(self, name: str, languages: set[str]) -> None:
        self.name = name
        self.languages = languages
        self.calls: list[tuple[str, str]] = []

    def supports(self, language: str) -> bool:
        return language in self.languages

    def default_voice_for_language(self, language: str) -> str:
        return f"{self.name}-{language.replace('zh-Hans', 'zh')}"

    def voice_catalog(self) -> dict[str, list[dict]]:
        return {
            language: [
                {
                    "id": self.default_voice_for_language(language),
                    "name": f"{self.name} {language}",
                    "short_name": self.default_voice_for_language(language),
                    "display_name": f"{self.name} {language}",
                    "gender": "unknown",
                    "provider": "local",
                    "language": language,
                    "experimental": True,
                }
            ]
            for language in self.languages
        }

    def status(self) -> dict:
        return {
            "ready": True,
            "status": "ready",
            "enabled": True,
            "missing": [],
            "engine": self.name,
            "content_type": "audio/wav",
            "output_format": "wav",
        }

    def status_for_language(self, language: str) -> dict:
        return self.status()

    async def synthesize(self, text: str, language: str, voice: str | None = None, audio_format: str | None = None):
        self.calls.append((text, language))
        return b"RIFFfake-local", "audio/wav", 250
