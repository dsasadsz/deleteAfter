import pytest
import shutil
import subprocess
import textwrap
import wave
from io import BytesIO
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.mark.asyncio
async def test_mock_tts_synthesize_returns_audio_bytes_and_metadata():
    from app.tts.mock_tts import MockTTS

    provider = MockTTS()
    result = await provider.synthesize("Сәлем", "kk")

    assert result.audio_bytes.startswith(b"RIFF")
    assert result.content_type == "audio/wav"
    assert result.language == "kk"
    assert result.provider == "mock"
    assert result.text_chars == 5
    assert result.cached is False
    assert result.latency_ms >= 0
    assert result.metadata["mock"] is True


@pytest.mark.asyncio
async def test_mock_tts_synthesize_returns_audible_wav_tone():
    from app.tts.mock_tts import MockTTS

    provider = MockTTS()
    result = await provider.synthesize("Сәлем", "kk")

    with wave.open(BytesIO(result.audio_bytes), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 16000
        assert reader.getnframes() >= 8000
        frames = reader.readframes(reader.getnframes())

    assert len(result.audio_bytes) > 16000
    assert any(byte != 0 for byte in frames)
    assert result.duration_ms >= 500


def test_create_tts_provider_returns_mock_provider():
    from app.tts.factory import create_tts_provider
    provider = create_tts_provider("mock")
    assert provider.name == "mock"


@pytest.mark.asyncio
async def test_azure_tts_missing_key_raises_clear_configuration_error():
    from app.tts.azure_tts import AzureTTS
    from app.tts.base import TTSConfigurationError

    provider = AzureTTS(api_key="", region="eastus", voices={"kk": "kk-KZ-AigulNeural"})

    with pytest.raises(TTSConfigurationError) as exc:
        await provider.synthesize("Сәлем", "kk")

    assert "AZURE_TTS_KEY" in str(exc.value)


def test_azure_tts_status_reports_missing_voice_and_key():
    from app.tts.azure_tts import AzureTTS

    provider = AzureTTS(api_key="", region="", voices={"kk": ""})
    status = provider.status()

    assert status["ready"] is False
    assert "AZURE_TTS_KEY" in status["missing"]
    assert "AZURE_TTS_REGION" in status["missing"]
    assert "AZURE_TTS_DEFAULT_VOICE_KK" in status["missing"]


@pytest.mark.asyncio
async def test_azure_tts_synthesize_uses_required_user_agent_and_request_metadata():
    from app.tts.azure_tts import AzureTTS

    class FakeResponse:
        status_code = 200
        content = b"mp3-audio"
        headers = {"X-RequestId": "azure-request-123"}

    class FakeHttpClient:
        def __init__(self):
            self.headers = None

        async def post(self, url, content, headers):
            self.headers = headers
            return FakeResponse()

    http_client = FakeHttpClient()
    provider = AzureTTS(
        api_key="key",
        region="eastus",
        voices={"kk": "kk-KZ-AigulNeural"},
        http_client=http_client,
    )

    result = await provider.synthesize("Сәлем", "kk", metadata={"source": "test"})

    assert http_client.headers["User-Agent"] == "live_translation_platform"
    assert result.metadata["azure_request_id"] == "azure-request-123"
    assert result.metadata["source"] == "test"


@pytest.mark.asyncio
async def test_azure_tts_riff_audio_format_returns_wav_content_type():
    from app.tts.azure_tts import AzureTTS

    class FakeResponse:
        status_code = 200
        content = b"riff-audio"
        headers = {}

    class FakeHttpClient:
        async def post(self, url, content, headers):
            return FakeResponse()

    provider = AzureTTS(
        api_key="key",
        region="eastus",
        voices={"kk": "kk-KZ-AigulNeural"},
        http_client=FakeHttpClient(),
    )

    result = await provider.synthesize("Сәлем", "kk", audio_format="riff-16khz-16bit-mono-pcm")

    assert result.content_type == "audio/wav"


@pytest.mark.asyncio
async def test_tts_cache_returns_cached_result_on_repeated_text():
    from app.tts.cache import TTSCache, synthesize_with_cache
    from app.tts.mock_tts import MockTTS

    provider = MockTTS()
    cache = TTSCache(max_items=10)

    first = await synthesize_with_cache(cache, provider, "Сәлем", "kk", None, "audio/wav")
    second = await synthesize_with_cache(cache, provider, "Сәлем", "kk", None, "audio/wav")

    assert first.cached is False
    assert second.cached is True
    assert second.audio_bytes == first.audio_bytes


def test_tts_cache_default_max_items_is_500():
    from app.tts.cache import TTSCache

    cache = TTSCache()

    assert cache.max_items == 500


def test_tts_cache_key_uses_pipe_separated_fields():
    from hashlib import sha256

    from app.tts.cache import tts_cache_key

    text = "Сәлем"
    key = tts_cache_key("mock", "kk", None, "audio/wav", text)

    parts = key.split("|")
    assert parts == ["mock", "kk", "", "audio%2Fwav", sha256(text.encode("utf-8")).hexdigest()]


def test_tts_cache_key_avoids_pipe_delimiter_collisions():
    from app.tts.cache import tts_cache_key

    first = tts_cache_key("mock", "kk", "a|b", "c", "Сәлем")
    second = tts_cache_key("mock", "kk", "a", "b|c", "Сәлем")

    assert first != second


@pytest.mark.asyncio
async def test_tts_cache_hit_uses_current_metadata_without_leaking_first_request():
    from app.tts.cache import TTSCache, synthesize_with_cache
    from app.tts.mock_tts import MockTTS

    provider = MockTTS()
    cache = TTSCache(max_items=10)

    await synthesize_with_cache(
        cache,
        provider,
        "Сәлем",
        "kk",
        None,
        "audio/wav",
        metadata={"caption_id": "first"},
    )
    second = await synthesize_with_cache(
        cache,
        provider,
        "Сәлем",
        "kk",
        None,
        "audio/wav",
        metadata={"caption_id": "second"},
    )

    assert second.cached is True
    assert second.metadata["mock"] is True
    assert second.metadata["caption_id"] == "second"


def test_tts_status_ready_false_when_azure_missing_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_TTS_KEY", "")
    monkeypatch.setenv("AZURE_TTS_REGION", "")
    monkeypatch.setenv("AZURE_TTS_ENDPOINT", "")
    monkeypatch.setenv("AZURE_TTS_DEFAULT_VOICE_KK", "")
    app = _app(tmp_path, monkeypatch, "tts-status.db", provider="azure")
    with TestClient(app) as client:
        response = client.get("/api/tts/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["provider"] == "azure"
    assert payload["ready"] is False
    assert "kk" in payload["supported_languages"]
    assert "AZURE_TTS_KEY" in payload["missing"]


def test_tts_status_invalid_provider_returns_structured_not_ready(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-invalid-status.db", provider="invalid")
    with TestClient(app) as client:
        response = client.get("/api/tts/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["provider"] == "invalid"
    assert payload["ready"] is False
    assert "kk" in payload["supported_languages"]
    assert payload["voices"] == {}
    assert any("Unknown TTS provider" in item or "invalid" in item.lower() for item in payload["missing"])


def test_student_tts_js_displays_missing_configuration_details(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-js-status.db", provider="azure")
    with TestClient(app) as client:
        response = client.get("/static/student_tts.js")

    assert response.status_code == 200
    assert "payload.missing" in response.text
    assert "AZURE_TTS_DEFAULT_VOICE_KK" in response.text


def test_tts_synthesize_rejects_empty_text(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-empty.db", provider="mock")
    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(f"/api/lessons/{lesson['lesson_id']}/tts/synthesize", json={"text": "", "language": "kk"})
    assert response.status_code == 400


def test_tts_synthesize_rejects_too_long_text(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_MAX_TEXT_CHARS", "5")
    app = _app(tmp_path, monkeypatch, "tts-long.db", provider="mock")
    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(f"/api/lessons/{lesson['lesson_id']}/tts/synthesize", json={"text": "too long", "language": "kk"})
    assert response.status_code == 400


def test_tts_synthesize_returns_audio_response_with_headers_using_mock(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-audio.db", provider="mock")
    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(f"/api/lessons/{lesson['lesson_id']}/tts/synthesize", json={"text": "Сәлем", "language": "kk"})
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"RIFF")
    assert len(response.content) > 16000
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.headers["x-tts-provider"] == "mock"
    assert response.headers["x-tts-language"] == "kk"
    assert response.headers["x-tts-cached"] == "false"
    assert response.headers["x-tts-voice"] == "mock-kk-1"
    assert int(response.headers["x-tts-latency-ms"]) >= 0


def test_tts_synthesize_sanitizes_voice_response_header(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-unsafe-voice.db", provider="mock")
    with TestClient(app, raise_server_exceptions=False) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"text": "Сәлем", "language": "kk", "voice": "mock-kk\r\nX-Bad: injected"},
        )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VOICE_NOT_AVAILABLE_FOR_LANGUAGE"


def test_tts_synthesize_request_does_not_expose_audio_format():
    from app.tts.schemas import TTSSynthesizeRequest

    assert "audio_format" not in TTSSynthesizeRequest.model_fields


def test_tts_synthesize_invalid_provider_returns_503(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-invalid-synthesize.db", provider="invalid")
    with TestClient(app, raise_server_exceptions=False) as client:
        lesson = _create_lesson(client)
        response = client.post(f"/api/lessons/{lesson['lesson_id']}/tts/synthesize", json={"text": "Сәлем", "language": "kk"})
    assert response.status_code == 503


def test_tts_status_response_field_order_matches_spec():
    from app.tts.schemas import TTSStatusResponse

    assert list(TTSStatusResponse.model_fields) == [
        "enabled",
        "provider",
        "active_provider",
        "ready",
        "missing",
        "supported_languages",
        "voices",
        "default_voice_by_language",
        "providers",
        "selected_voice_support",
        "shared_cache_enabled",
        "audio_url_enabled",
    ]


def test_tts_synthesize_repeated_text_sets_cached_header(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-cache-api.db", provider="mock")
    with TestClient(app) as client:
        lesson = _create_lesson(client)
        url = f"/api/lessons/{lesson['lesson_id']}/tts/synthesize"
        first = client.post(url, json={"text": "Сәлем", "language": "kk"})
        second = client.post(url, json={"text": "Сәлем", "language": "kk"})
    assert first.headers["x-tts-cached"] == "false"
    assert second.headers["x-tts-cached"] == "true"


def test_tts_synthesize_auth_accepts_tts_play_scope(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-auth-play.db", provider="mock", websocket_auth_enabled=True)
    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _token("student-1", "student", lesson["lesson_id"], ["tts:play"])
        response = client.post(f"/api/lessons/{lesson['lesson_id']}/tts/synthesize?token={token}", json={"text": "Сәлем", "language": "kk"})
    assert response.status_code == 200, response.text


def test_tts_synthesize_auth_accepts_captions_read_scope(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-auth-captions.db", provider="mock", websocket_auth_enabled=True)
    with TestClient(app) as client:
        lesson = _create_lesson(client)
        token = _token("student-1", "student", lesson["lesson_id"], ["captions:read"])
        response = client.post(f"/api/lessons/{lesson['lesson_id']}/tts/synthesize?token={token}", json={"text": "Сәлем", "language": "kk"})
    assert response.status_code == 200, response.text


def test_tts_synthesize_auth_rejects_missing_and_wrong_scope(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-auth-reject.db", provider="mock", websocket_auth_enabled=True)
    with TestClient(app) as client:
        lesson = _create_lesson(client)
        missing = client.post(f"/api/lessons/{lesson['lesson_id']}/tts/synthesize", json={"text": "Сәлем", "language": "kk"})
        wrong_token = _token("student-1", "student", lesson["lesson_id"], ["zoom:embed"])
        wrong = client.post(f"/api/lessons/{lesson['lesson_id']}/tts/synthesize?token={wrong_token}", json={"text": "Сәлем", "language": "kk"})
    assert missing.status_code == 401
    assert wrong.status_code == 403


def test_captions_js_exposes_language_helpers_and_no_translation_fallback(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "caption-helper.db", provider="mock")
    with TestClient(app) as client:
        response = client.get("/static/captions.js")
    assert response.status_code == 200
    text = response.text
    assert "window.CaptionRendering" in text
    assert "function captionTextForLanguage" in text
    assert "function ttsTextForLanguage" in text
    assert "Translation unavailable for ${language}" in text
    assert "client_caption_received_at = Date.now()" in text


def test_student_page_renders_tts_panel_and_script(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-ui.db", provider="mock")
    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.get(f"/student/{lesson['lesson_id']}")
    assert response.status_code == 200
    assert "Voice translation" in response.text
    assert "Mock TTS is test audio, not real voice." in response.text
    assert "TTS reads live captions only. Backlog: last 5 seconds." in response.text
    assert "Show full transcript" in response.text
    assert "Show history" in response.text
    assert f"/lessons/{lesson['lesson_id']}/transcript" in response.text
    assert "ttsAutoplay" in response.text
    assert "Lower Zoom audio during TTS" in response.text
    assert "student_tts.js" in response.text


def test_student_tts_js_contains_final_caption_and_queue_guards(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-js.db", provider="mock")
    with TestClient(app) as client:
        response = client.get("/static/student_tts.js")
    assert response.status_code == 200
    assert "payload.is_partial" in response.text
    assert "ttsEnabledAtMs" in response.text
    assert "ttsLiveBacklogMs: 5000" in response.text
    assert "Skipped old caption" in response.text
    assert "latest_only" in response.text
    assert "ttsTextForLanguage" in response.text
    assert "/tts/synthesize" in response.text
    assert "audio response received" in response.text
    assert "play started" in response.text
    assert "play ended" in response.text
    assert "play blocked by browser" in response.text


def test_student_tts_js_aborts_stale_synthesis_requests(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-js-abort.db", provider="mock")
    with TestClient(app) as client:
        response = client.get("/static/student_tts.js")
    assert response.status_code == 200
    text = response.text
    assert "requestController" in text
    assert "new AbortController()" in text
    assert "signal: requestController.signal" in text
    assert "requestController.abort()" in text
    assert "playbackGeneration" in text
    assert "playbackGeneration += 1" in text


def test_student_tts_js_revokes_object_urls_on_stop_and_failed_playback(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "tts-js-url-cleanup.db", provider="mock")
    with TestClient(app) as client:
        response = client.get("/static/student_tts.js")
    assert response.status_code == 200
    text = response.text
    assert "currentObjectUrl" in text
    assert "function revokeCurrentObjectUrl()" in text
    assert "revokeCurrentObjectUrl();\n    ttsState.queue = []" in text
    assert "let objectUrl = null" in text
    assert "if (objectUrl) URL.revokeObjectURL(objectUrl)" in text
    assert "if (objectUrl === ttsState.currentObjectUrl) {\n        revokeCurrentObjectUrl();" in text


def test_student_tts_js_skips_duplicate_final_playback():
    if shutil.which("node") is None:
        pytest.skip("node is required for student_tts.js duplicate playback test")

    captions_js = Path("app/web/static/captions.js").read_text(encoding="utf-8")
    tts_js = Path("app/web/static/student_tts.js").read_text(encoding="utf-8")
    script = f"""
    const assert = require("assert");

    function element(overrides = {{}}) {{
      return {{
        dataset: {{}},
        value: "",
        checked: false,
        disabled: false,
        textContent: "",
        hidden: false,
        addEventListener() {{}},
        ...overrides,
      }};
    }}

    const fakeTtsPanel = element({{
      dataset: {{
        lessonId: "lesson-tts",
        autoplayDefault: "true",
        queueMode: "sequential",
        volumeDefault: "1",
        duckingEnabled: "false",
      }},
    }});
    const nodes = {{
      "#ttsStatus": element(),
      "#ttsEnabled": element({{ checked: true }}),
      "#ttsAutoplay": element({{ checked: true }}),
      "#ttsLanguage": element({{ value: "kk" }}),
      "#ttsQueueMode": element({{ value: "sequential" }}),
      "#ttsVolume": element({{ value: "1" }}),
      "#ttsDuckingEnabled": element({{ checked: false }}),
      "#ttsDuckingStatus": element(),
      "#ttsDuckingFallback": element(),
      "#ttsPlayLatest": element(),
      "#ttsStop": element(),
      "#ttsQueued": element(),
      "#ttsRequests": element(),
      "#ttsErrors": element(),
      "#ttsCacheHits": element(),
      "#ttsLatency": element(),
      "#ttsLastError": element(),
    }};

    global.window = {{
      location: {{ search: "", protocol: "http:", host: "testserver", origin: "http://testserver" }},
      addEventListener() {{}},
      CaptionState: {{ selectedLanguage: () => "kk" }},
    }};
    global.document = {{
      querySelector(query) {{
        if (query === ".student-layout") return null;
        if (query === ".student-tts-panel") return fakeTtsPanel;
        return nodes[query] || element();
      }},
      querySelectorAll() {{ return []; }},
      createElement() {{ return element(); }},
    }};
    global.URL.createObjectURL = () => "blob:tts";
    global.URL.revokeObjectURL = () => {{}};
    global.window.ZoomAudioDucking = {{ duck: () => ({{ controllable: false }}), restore() {{}} }};

    class FakeAudio {{
      constructor() {{
        FakeAudio.instances.push(this);
        this.volume = 1;
        this.onended = null;
        this.onerror = null;
      }}
      pause() {{}}
      removeAttribute() {{}}
      load() {{}}
      play() {{ return Promise.resolve(); }}
    }}
    FakeAudio.instances = [];
    global.Audio = FakeAudio;

    let synthesizeFetches = 0;
    global.fetch = async (url, options = {{}}) => {{
      if (String(url).endsWith("/api/tts/status")) {{
        return {{
          ok: true,
          json: async () => ({{ enabled: true, ready: true, provider: "mock", missing: [] }}),
        }};
      }}
      synthesizeFetches += 1;
      return {{
        ok: true,
        headers: {{ get(name) {{ return name === "x-tts-latency-ms" ? "1" : "false"; }} }},
        blob: async () => ({{}}),
      }};
    }};

    {captions_js}
    {tts_js}

    (async () => {{
    const payload = {{
      lesson_id: "lesson-tts",
      caption_id: "caption-1",
      text_hash: "hash-1",
      is_partial: false,
      is_final: true,
      original_text: "same",
      original_text_normalized: "same",
      translations: {{ kk: "бірдей" }},
    }};

    window.StudentTTS.onFinalCaptionForTts(payload);
    window.StudentTTS.onFinalCaptionForTts({{ ...payload, latency_ms: {{ total: 50 }} }});
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.strictEqual(synthesizeFetches, 1);
    FakeAudio.instances[0].onended();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.strictEqual(synthesizeFetches, 1);
    assert.match(nodes["#ttsStatus"].textContent, /TTS skipped duplicate final|play ended/);
    }})();
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_tts_js_skips_unavailable_or_waiting_translation_texts():
    if shutil.which("node") is None:
        pytest.skip("node is required for student_tts.js translation availability test")

    captions_js = Path("app/web/static/captions.js").read_text(encoding="utf-8")
    tts_js = Path("app/web/static/student_tts.js").read_text(encoding="utf-8")
    script = f"""
    const assert = require("assert");

    function element(overrides = {{}}) {{
      return {{
        dataset: {{}},
        value: "",
        checked: false,
        disabled: false,
        textContent: "",
        hidden: false,
        addEventListener() {{}},
        ...overrides,
      }};
    }}

    const fakeTtsPanel = element({{
      dataset: {{
        lessonId: "lesson-tts",
        autoplayDefault: "true",
        queueMode: "sequential",
        volumeDefault: "1",
        duckingEnabled: "false",
      }},
    }});
    const nodes = {{
      "#ttsStatus": element(),
      "#ttsEnabled": element({{ checked: true }}),
      "#ttsAutoplay": element({{ checked: true }}),
      "#ttsLanguage": element({{ value: "kk" }}),
      "#ttsQueueMode": element({{ value: "sequential" }}),
      "#ttsVolume": element({{ value: "1" }}),
      "#ttsDuckingEnabled": element({{ checked: false }}),
      "#ttsDuckingStatus": element(),
      "#ttsDuckingFallback": element(),
      "#ttsPlayLatest": element(),
      "#ttsStop": element(),
      "#ttsQueued": element(),
      "#ttsRequests": element(),
      "#ttsErrors": element(),
      "#ttsCacheHits": element(),
      "#ttsLatency": element(),
      "#ttsLastError": element(),
    }};

    global.window = {{
      location: {{ search: "", protocol: "http:", host: "testserver", origin: "http://testserver" }},
      addEventListener() {{}},
      CaptionState: {{ selectedLanguage: () => nodes["#ttsLanguage"].value }},
    }};
    global.document = {{
      querySelector(query) {{
        if (query === ".student-layout") return null;
        if (query === ".student-tts-panel") return fakeTtsPanel;
        return nodes[query] || element();
      }},
      querySelectorAll() {{ return []; }},
      createElement() {{ return element(); }},
    }};
    global.URL.createObjectURL = () => "blob:tts";
    global.URL.revokeObjectURL = () => {{}};
    global.window.ZoomAudioDucking = {{ duck: () => ({{ controllable: false }}), restore() {{}} }};

    class FakeAudio {{
      constructor() {{
        FakeAudio.instances.push(this);
        this.volume = 1;
        this.onended = null;
        this.onerror = null;
      }}
      pause() {{}}
      removeAttribute() {{}}
      load() {{}}
      play() {{ return Promise.resolve(); }}
    }}
    FakeAudio.instances = [];
    global.Audio = FakeAudio;

    const synthesized = [];
    global.fetch = async (url, options = {{}}) => {{
      if (String(url).endsWith("/api/tts/status")) {{
        return {{
          ok: true,
          json: async () => ({{ enabled: true, ready: true, provider: "mock", missing: [] }}),
        }};
      }}
      synthesized.push(JSON.parse(options.body));
      return {{
        ok: true,
        headers: {{ get(name) {{ return name === "x-tts-latency-ms" ? "1" : "false"; }} }},
        blob: async () => ({{}}),
      }};
    }};

    {captions_js}
    {tts_js}

    (async () => {{
    const base = {{
      lesson_id: "lesson-tts",
      is_partial: false,
      is_final: true,
      original_text: "Russian original",
      original_text_normalized: "Russian normalized",
    }};

    window.StudentTTS.onFinalCaptionForTts({{ ...base, caption_id: "missing", translations: {{}} }});
    window.StudentTTS.onFinalCaptionForTts({{ ...base, caption_id: "unavailable", translations: {{ kk: "Translation unavailable for kk" }} }});
    window.StudentTTS.onFinalCaptionForTts({{ ...base, caption_id: "waiting", translations: {{ kk: "Waiting for kk translation..." }} }});
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.strictEqual(synthesized.length, 0);

    window.StudentTTS.onFinalCaptionForTts({{ ...base, caption_id: "real-kk", translations: {{ kk: "Kazakh real" }} }});
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepStrictEqual(synthesized, [{{ text: "Kazakh real", language: "kk", provider: "mock", caption_id: "real-kk" }}]);
    FakeAudio.instances[0].onended();

    nodes["#ttsLanguage"].value = "ru";
    window.StudentTTS.onFinalCaptionForTts({{ ...base, caption_id: "real-ru", translations: {{}} }});
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepStrictEqual(synthesized[1], {{ text: "Russian normalized", language: "ru", provider: "mock", caption_id: "real-ru" }});
    }})();
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_tts_js_skips_old_caption_and_allows_live_backlog():
    if shutil.which("node") is None:
        pytest.skip("node is required for student_tts.js live backlog test")

    script = _student_tts_live_harness(
        """
        now = 100_000;
        nodes["#ttsEnabled"].checked = true;
        nodes["#ttsEnabled"].handlers.change({ target: nodes["#ttsEnabled"] });

        window.StudentTTS.onFinalCaptionForTts(payload({
          caption_id: "stale",
          translations: { kk: "too old" },
          timestamps: { websocket_sent_at: new Date(now - 6_000).toISOString() },
        }));
        await tick();
        assert.strictEqual(synthesized.length, 0);
        assert.match(nodes["#ttsStatus"].textContent, /Skipped old caption/);

        window.StudentTTS.onFinalCaptionForTts(payload({
          caption_id: "fresh",
          translations: { kk: "fresh enough" },
          timestamps: { websocket_sent_at: new Date(now - 3_000).toISOString() },
        }));
        await tick();
        assert.strictEqual(synthesized.length, 1);
        assert.strictEqual(synthesized[0].text, "fresh enough");
        """
    )

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_tts_js_toggle_clears_sequential_queue():
    if shutil.which("node") is None:
        pytest.skip("node is required for student_tts.js queue clearing test")

    script = _student_tts_live_harness(
        """
        audioPlayPromise = new Promise(() => {});
        now = 200_000;
        nodes["#ttsEnabled"].checked = true;
        nodes["#ttsEnabled"].handlers.change({ target: nodes["#ttsEnabled"] });

        window.StudentTTS.onFinalCaptionForTts(payload({
          caption_id: "playing",
          translations: { kk: "currently playing" },
          timestamps: { websocket_sent_at: new Date(now).toISOString() },
        }));
        await tick();
        window.StudentTTS.onFinalCaptionForTts(payload({
          caption_id: "queued",
          translations: { kk: "queued stale after toggle" },
          timestamps: { websocket_sent_at: new Date(now).toISOString() },
        }));
        await tick();
        assert.strictEqual(nodes["#ttsQueued"].textContent, "1");

        nodes["#ttsEnabled"].checked = false;
        nodes["#ttsEnabled"].handlers.change({ target: nodes["#ttsEnabled"] });
        assert.strictEqual(nodes["#ttsQueued"].textContent, "0");

        now += 30_000;
        nodes["#ttsEnabled"].checked = true;
        nodes["#ttsEnabled"].handlers.change({ target: nodes["#ttsEnabled"] });
        assert.strictEqual(nodes["#ttsQueued"].textContent, "0");
        assert.match(nodes["#ttsStatus"].textContent, /live captions only/);
        """
    )

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_tts_js_play_latest_does_not_play_old_backlog():
    if shutil.which("node") is None:
        pytest.skip("node is required for student_tts.js play latest backlog test")

    script = _student_tts_live_harness(
        """
        now = 300_000;
        window.StudentTTS.onFinalCaptionForTts(payload({
          caption_id: "old-latest",
          translations: { kk: "old latest" },
          timestamps: { websocket_sent_at: new Date(now - 60_000).toISOString() },
        }));

        nodes["#ttsEnabled"].checked = true;
        nodes["#ttsEnabled"].handlers.change({ target: nodes["#ttsEnabled"] });
        nodes["#ttsPlayLatest"].handlers.click();
        await tick();

        assert.strictEqual(synthesized.length, 0);
        assert.match(nodes["#ttsStatus"].textContent, /Skipped old caption/);
        """
    )

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_tts_js_sends_voice_selection_fields():
    if shutil.which("node") is None:
        pytest.skip("node is required for student_tts.js voice selection test")

    captions_js = Path("app/web/static/captions.js").read_text(encoding="utf-8")
    tts_js = Path("app/web/static/student_tts.js").read_text(encoding="utf-8")
    script = f"""
    const assert = require("assert");

    function element(overrides = {{}}) {{
      return {{
        dataset: {{}},
        value: "",
        checked: false,
        disabled: false,
        textContent: "",
        hidden: false,
        options: [],
        appendChild(child) {{ this.options.push(child); }},
        addEventListener() {{}},
        ...overrides,
      }};
    }}

    const fakeTtsPanel = element({{
      dataset: {{
        lessonId: "lesson-tts",
        autoplayDefault: "true",
        queueMode: "sequential",
        volumeDefault: "1",
        duckingEnabled: "false",
      }},
    }});
    const nodes = {{
      "#ttsStatus": element(),
      "#ttsEnabled": element({{ checked: true }}),
      "#ttsAutoplay": element({{ checked: true }}),
      "#ttsLanguage": element({{ value: "kk" }}),
      "#ttsProvider": element({{ value: "azure" }}),
      "#ttsVoice": element({{ value: "kk-female" }}),
      "#ttsVoiceStatus": element(),
      "#ttsQueueMode": element({{ value: "sequential" }}),
      "#ttsVolume": element({{ value: "1" }}),
      "#ttsDuckingEnabled": element({{ checked: false }}),
      "#ttsDuckingStatus": element(),
      "#ttsDuckingFallback": element(),
      "#ttsPlayLatest": element(),
      "#ttsStop": element(),
      "#ttsQueued": element(),
      "#ttsRequests": element(),
      "#ttsErrors": element(),
      "#ttsCacheHits": element(),
      "#ttsLatency": element(),
      "#ttsLastError": element(),
    }};

    global.window = {{
      location: {{ search: "", protocol: "http:", host: "testserver", origin: "http://testserver" }},
      addEventListener() {{}},
      CaptionState: {{ selectedLanguage: () => "kk" }},
    }};
    global.document = {{
      querySelector(query) {{
        if (query === ".student-layout") return null;
        if (query === ".student-tts-panel") return fakeTtsPanel;
        return nodes[query] || element();
      }},
      querySelectorAll() {{ return []; }},
      createElement(tag) {{
        return element({{ tagName: tag.toUpperCase() }});
      }},
    }};
    global.URL.createObjectURL = () => "blob:tts";
    global.URL.revokeObjectURL = () => {{}};
    global.window.ZoomAudioDucking = {{ duck: () => ({{ controllable: false }}), restore() {{}} }};

    class FakeAudio {{
      constructor() {{
        FakeAudio.instances.push(this);
        this.volume = 1;
        this.onended = null;
        this.onerror = null;
      }}
      pause() {{}}
      removeAttribute() {{}}
      load() {{}}
      play() {{ return Promise.resolve(); }}
    }}
    FakeAudio.instances = [];
    global.Audio = FakeAudio;

    const synthesized = [];
    global.fetch = async (url, options = {{}}) => {{
      if (String(url).endsWith("/api/tts/status")) {{
        return {{
          ok: true,
          json: async () => ({{
            enabled: true,
            ready: true,
            provider: "azure",
            missing: [],
            voices: {{ kk: [{{ id: "kk-female", name: "Kazakh Female", gender: "female", provider: "azure", language: "kk" }}] }},
            default_voice_by_language: {{ kk: "kk-female" }},
            selected_voice_support: {{ provider_override: false, providers: {{ azure: {{ status: "ready", ready: true }} }} }},
          }}),
        }};
      }}
      synthesized.push(JSON.parse(options.body));
      return {{
        ok: true,
        headers: {{ get(name) {{ return name === "x-tts-latency-ms" ? "1" : "false"; }} }},
        blob: async () => ({{}}),
      }};
    }};

    {captions_js}
    {tts_js}

    (async () => {{
      await new Promise((resolve) => setTimeout(resolve, 0));
      window.StudentTTS.onFinalCaptionForTts({{
        lesson_id: "lesson-tts",
        caption_id: "voice-selection",
        is_partial: false,
        is_final: true,
        original_text: "Russian",
        original_text_normalized: "Russian",
        translations: {{ kk: "Kazakh real" }},
      }});
      await new Promise((resolve) => setTimeout(resolve, 0));
      assert.deepStrictEqual(synthesized[0], {{
        text: "Kazakh real",
        language: "kk",
        provider: "azure",
        voice: "kk-female",
        caption_id: "voice-selection",
      }});
    }})();
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_stage20_docs_and_env_examples_include_tts_settings():
    env = open(".env.example", encoding="utf-8").read()
    project_report = open("docs/PROJECT_REPORT.md", encoding="utf-8").read()
    architecture = open("docs/ARCHITECTURE.md", encoding="utf-8").read()

    assert "TTS_ENABLED=true" in env
    assert "AZURE_TTS_KEY=" in env
    assert "## 10. TTS" in project_report
    assert "browser autoplay" in project_report.lower()
    assert "TTS output" in architecture
    assert "client-requested" in architecture


def _token(sub: str, role: str, lesson_id: str, scopes: list[str]) -> str:
    from app.security.tokens import create_access_token

    return create_access_token(
        {"sub": sub, "role": role, "lesson_id": lesson_id, "external_lesson_id": "ext-stage20", "scopes": scopes},
        ttl_seconds=3600,
    )


def _run_node_script(script: str):
    return subprocess.run(["node", "-"], input=textwrap.dedent(script), capture_output=True, text=True, encoding="utf-8", timeout=10)


def _student_tts_live_harness(test_body: str) -> str:
    captions_js = Path("app/web/static/captions.js").read_text(encoding="utf-8")
    tts_js = Path("app/web/static/student_tts.js").read_text(encoding="utf-8")
    return f"""
    const assert = require("assert");

    function element(overrides = {{}}) {{
      return {{
        dataset: {{}},
        value: "",
        checked: false,
        disabled: false,
        textContent: "",
        hidden: false,
        options: [],
        handlers: {{}},
        appendChild(child) {{ this.options.push(child); }},
        addEventListener(event, handler) {{ this.handlers[event] = handler; }},
        ...overrides,
      }};
    }}

    const fakeTtsPanel = element({{
      dataset: {{
        lessonId: "lesson-tts",
        autoplayDefault: "true",
        queueMode: "sequential",
        volumeDefault: "1",
        duckingEnabled: "false",
      }},
    }});
    const nodes = {{
      "#ttsStatus": element(),
      "#ttsEnabled": element({{ checked: false }}),
      "#ttsAutoplay": element({{ checked: true }}),
      "#ttsLanguage": element({{ value: "kk" }}),
      "#ttsProvider": element({{ value: "mock" }}),
      "#ttsVoice": element({{ value: "" }}),
      "#ttsVoiceStatus": element(),
      "#ttsQueueMode": element({{ value: "sequential" }}),
      "#ttsVolume": element({{ value: "1" }}),
      "#ttsDuckingEnabled": element({{ checked: false }}),
      "#ttsDuckingStatus": element(),
      "#ttsDuckingFallback": element(),
      "#ttsPlayLatest": element(),
      "#ttsStop": element(),
      "#ttsQueued": element(),
      "#ttsRequests": element(),
      "#ttsErrors": element(),
      "#ttsCacheHits": element(),
      "#ttsLatency": element(),
      "#ttsLastError": element(),
    }};

    let now = 0;
    Date.now = () => now;
    const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
    global.window = {{
      location: {{ search: "", protocol: "http:", host: "testserver", origin: "http://testserver" }},
      addEventListener() {{}},
      CaptionState: {{ selectedLanguage: () => nodes["#ttsLanguage"].value }},
    }};
    global.document = {{
      querySelector(query) {{
        if (query === ".student-layout") return null;
        if (query === ".student-tts-panel") return fakeTtsPanel;
        return nodes[query] || element();
      }},
      querySelectorAll() {{ return []; }},
      createElement(tag) {{ return element({{ tagName: tag.toUpperCase() }}); }},
    }};
    global.URL.createObjectURL = () => "blob:tts";
    global.URL.revokeObjectURL = () => {{}};
    global.window.ZoomAudioDucking = {{ duck: () => ({{ controllable: false }}), restore() {{}} }};

    let audioPlayPromise = Promise.resolve();
    class FakeAudio {{
      constructor() {{
        FakeAudio.instances.push(this);
        this.volume = 1;
        this.onended = null;
        this.onerror = null;
      }}
      pause() {{}}
      removeAttribute() {{}}
      load() {{}}
      play() {{ return audioPlayPromise; }}
    }}
    FakeAudio.instances = [];
    global.Audio = FakeAudio;

    const synthesized = [];
    global.fetch = async (url, options = {{}}) => {{
      if (String(url).endsWith("/api/tts/status")) {{
        return {{
          ok: true,
          json: async () => ({{ enabled: true, ready: true, provider: "mock", missing: [] }}),
        }};
      }}
      synthesized.push(JSON.parse(options.body));
      return {{
        ok: true,
        headers: {{ get(name) {{ return name === "x-tts-latency-ms" ? "1" : "false"; }} }},
        blob: async () => ({{}}),
      }};
    }};

    function payload(overrides = {{}}) {{
      return {{
        lesson_id: "lesson-tts",
        caption_id: "caption-live",
        is_partial: false,
        is_final: true,
        original_text: "Russian",
        original_text_normalized: "Russian",
        translations: {{ kk: "Kazakh" }},
        ...overrides,
      }};
    }}

    {captions_js}
    {tts_js}

    (async () => {{
      await tick();
      {test_body}
    }})();
    """


def _app(tmp_path, monkeypatch, db_name: str, provider: str = "mock", websocket_auth_enabled: bool = False):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TTS_PROVIDER", provider)
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage20-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "true" if websocket_auth_enabled else "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "false" if websocket_auth_enabled else "true")
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post("/api/lessons", json={"title": "Stage 20", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock"})
    assert response.status_code == 201, response.text
    return response.json()
