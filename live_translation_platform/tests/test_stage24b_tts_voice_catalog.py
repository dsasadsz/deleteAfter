import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def test_tts_status_returns_provider_catalogs_grouped_by_language(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_TTS_KEY", "")
    monkeypatch.setenv("AZURE_TTS_REGION", "eastus")
    monkeypatch.setenv("AZURE_TTS_VOICES_KK", "kk-KZ-AigulNeural,kk-KZ-DauletNeural")
    monkeypatch.setenv("AZURE_TTS_VOICES_UZ", "uz-UZ-MadinaNeural")
    app = _app(tmp_path, monkeypatch, "stage24b-status.db", provider="azure")

    with TestClient(app) as client:
        response = client.get("/api/tts/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_provider"] == "azure"
    assert payload["providers"]["azure"]["ready"] is False
    assert payload["providers"]["azure"]["voices"]["kk"] == [
        {
            "id": "kk-KZ-AigulNeural",
            "name": "Aigul",
            "short_name": "kk-KZ-AigulNeural",
            "display_name": "Aigul",
            "gender": "female",
            "provider": "azure",
            "language": "kk",
            "locale": "kk-KZ",
            "experimental": False,
        },
        {
            "id": "kk-KZ-DauletNeural",
            "name": "Daulet",
            "short_name": "kk-KZ-DauletNeural",
            "display_name": "Daulet",
            "gender": "male",
            "provider": "azure",
            "language": "kk",
            "locale": "kk-KZ",
            "experimental": False,
        },
    ]
    assert payload["providers"]["mock"]["ready"] is True
    assert payload["providers"]["mock"]["voices"]["uz"][0]["language"] == "uz"


def test_mock_tts_status_returns_fixed_voice_catalog_without_gender_selector_ids(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage24b-mock-fixed-voices.db", provider="mock")

    with TestClient(app) as client:
        response = client.get("/api/tts/status")

    assert response.status_code == 200
    payload = response.json()
    assert [voice["id"] for voice in payload["providers"]["mock"]["voices"]["kk"]] == ["mock-kk-1", "mock-kk-2"]
    assert [voice["id"] for voice in payload["providers"]["mock"]["voices"]["uz"]] == ["mock-uz-1", "mock-uz-2"]
    assert [voice["id"] for voice in payload["providers"]["mock"]["voices"]["zh-Hans"]] == ["mock-zh-1", "mock-zh-2"]
    assert [voice["id"] for voice in payload["providers"]["mock"]["voices"]["ru"]] == ["mock-ru-1", "mock-ru-2"]


def test_elevenlabs_status_missing_key_is_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    app = _app(tmp_path, monkeypatch, "stage24b-elevenlabs-missing.db", provider="elevenlabs")

    with TestClient(app) as client:
        response = client.get("/api/tts/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["providers"]["elevenlabs"]["ready"] is False
    assert payload["providers"]["elevenlabs"]["status"] == "not_configured"
    assert payload["providers"]["elevenlabs"]["experimental"] is True
    assert payload["providers"]["elevenlabs"]["voices"]["kk"] == []


def test_elevenlabs_voice_discovery_groups_fake_client_results_by_supported_language():
    from app.tts.elevenlabs_tts import ElevenLabsTTS

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "voices": [
                    {"voice_id": "el-kk", "name": "Kazakh Voice", "labels": {"language": "kk", "gender": "female"}},
                    {"voice_id": "el-uz", "name": "Uzbek Voice", "labels": {"language": "uz", "gender": "male"}},
                    {"voice_id": "el-zh", "name": "Chinese Voice", "labels": {"locale": "zh-CN", "gender": "female"}},
                    {"voice_id": "el-en", "name": "English Voice", "labels": {"language": "en", "gender": "male"}},
                ]
            }

    class FakeClient:
        def get(self, url, headers=None, params=None):
            return FakeResponse()

    provider = ElevenLabsTTS(api_key="key", http_client=FakeClient())

    status = provider.status()

    assert status["ready"] is True
    assert [voice["id"] for voice in status["voices"]["kk"]] == ["el-kk"]
    assert [voice["id"] for voice in status["voices"]["uz"]] == ["el-uz"]
    assert [voice["id"] for voice in status["voices"]["zh-Hans"]] == ["el-zh"]
    assert status["voices"]["ru"] == []


def test_tts_synthesize_rejects_voice_from_wrong_language(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage24b-wrong-voice.db", provider="mock")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"provider": "mock", "language": "kk", "voice": "mock-uz-male", "text": "Salem"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VOICE_NOT_AVAILABLE_FOR_LANGUAGE"


def test_tts_synthesize_rejects_explicit_voice_when_language_catalog_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_TTS_KEY", "")
    monkeypatch.setenv("AZURE_TTS_REGION", "eastus")
    app = _app(tmp_path, monkeypatch, "stage24b-empty-catalog-voice.db", provider="azure")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"provider": "azure", "language": "kk", "voice": "kk-KZ-AigulNeural", "text": "Salem"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VOICE_NOT_AVAILABLE_FOR_LANGUAGE"


def test_tts_synthesize_uses_default_voice_for_language_when_voice_missing(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage24b-default-voice.db", provider="mock")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"provider": "mock", "language": "uz", "text": "Salem"},
        )

    assert response.status_code == 200, response.text
    assert response.headers["x-tts-provider"] == "mock"
    assert response.headers["x-tts-voice"] == "mock-uz-1"


def test_azure_voice_catalog_discovers_languages_missing_from_partial_config(monkeypatch):
    monkeypatch.delenv("IGNORE_DOTENV_IN_TESTS", raising=False)
    from app.tts.azure_tts import AzureTTS

    class FakeResponse:
        status_code = 200

        def json(self):
            return [
                {"ShortName": "kk-KZ-AigulNeural", "DisplayName": "Aigul", "LocalName": "Aigul local", "Gender": "Female", "Locale": "kk-KZ"},
                {"ShortName": "uz-UZ-MadinaNeural", "DisplayName": "Madina", "Gender": "Female", "Locale": "uz-UZ"},
                {"ShortName": "zh-CN-XiaoxiaoNeural", "DisplayName": "Xiaoxiao", "Gender": "Female", "Locale": "zh-CN"},
                {"ShortName": "ru-RU-DmitryNeural", "DisplayName": "Dmitry", "Gender": "Male", "Locale": "ru-RU"},
            ]

    class FakeClient:
        def get(self, url, headers=None):
            return FakeResponse()

    provider = AzureTTS(
        api_key="key",
        region="eastus",
        voice_lists={"kk": ["kk-KZ-AigulNeural"]},
        voice_list_client=FakeClient(),
    )

    catalog = provider.voice_catalog()

    assert [voice["id"] for voice in catalog["kk"]] == ["kk-KZ-AigulNeural"]
    assert catalog["kk"][0]["short_name"] == "kk-KZ-AigulNeural"
    assert catalog["kk"][0]["display_name"] == "Aigul"
    assert catalog["kk"][0]["local_name"] == "Aigul local"
    assert [voice["id"] for voice in catalog["uz"]] == ["uz-UZ-MadinaNeural"]
    assert [voice["id"] for voice in catalog["zh-Hans"]] == ["zh-CN-XiaoxiaoNeural"]
    assert [voice["id"] for voice in catalog["ru"]] == ["ru-RU-DmitryNeural"]


def test_azure_successful_discovery_does_not_mix_env_allowlists_per_language(monkeypatch):
    monkeypatch.delenv("IGNORE_DOTENV_IN_TESTS", raising=False)
    from app.tts.azure_tts import AzureTTS

    class FakeResponse:
        status_code = 200

        def json(self):
            return [
                {"ShortName": "uz-UZ-MadinaNeural", "DisplayName": "Madina", "Gender": "Female", "Locale": "uz-UZ"},
            ]

    class FakeClient:
        def get(self, url, headers=None):
            return FakeResponse()

    provider = AzureTTS(
        api_key="key",
        region="eastus-no-mix",
        voice_lists={"kk": ["kk-KZ-AigulNeural"]},
        voice_list_client=FakeClient(),
    )

    catalog = provider.voice_catalog()

    assert catalog["kk"] == []
    assert [voice["id"] for voice in catalog["uz"]] == ["uz-UZ-MadinaNeural"]


def test_azure_voice_catalog_groups_any_zh_locale_as_zh_hans(monkeypatch):
    monkeypatch.delenv("IGNORE_DOTENV_IN_TESTS", raising=False)
    from app.tts.azure_tts import AzureTTS

    class FakeResponse:
        status_code = 200

        def json(self):
            return [
                {"ShortName": "zh-HK-HiuMaanNeural", "DisplayName": "HiuMaan", "Gender": "Female", "Locale": "zh-HK"},
            ]

    class FakeClient:
        def get(self, url, headers=None):
            return FakeResponse()

    provider = AzureTTS(api_key="key", region="eastus-zh", voice_list_client=FakeClient())

    catalog = provider.voice_catalog()

    assert [voice["id"] for voice in catalog["zh-Hans"]] == ["zh-HK-HiuMaanNeural"]


def test_student_page_no_longer_renders_gender_filter(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage24b-no-gender-ui.db", provider="mock")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.get(f"/student/{lesson['lesson_id']}")

    assert response.status_code == 200
    assert "Gender filter" not in response.text
    assert "ttsVoiceGender" not in response.text


def test_student_tts_js_filters_voices_by_provider_and_language_without_gender():
    if shutil.which("node") is None:
        pytest.skip("node is required for student_tts.js voice catalog test")

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
        innerHTML: "",
        handlers: {{}},
        appendChild(child) {{ this.options.push(child); }},
        addEventListener(event, handler) {{ this.handlers[event] = handler; }},
        ...overrides,
      }};
    }}

    const fakeTtsPanel = element({{ dataset: {{ lessonId: "lesson-tts", autoplayDefault: "false", queueMode: "sequential", volumeDefault: "1", duckingEnabled: "false" }} }});
    const nodes = {{
      "#ttsStatus": element(),
      "#ttsEnabled": element({{ checked: true }}),
      "#ttsAutoplay": element({{ checked: false }}),
      "#ttsLanguage": element({{ value: "kk" }}),
      "#ttsProvider": element({{ value: "azure", options: [element({{ value: "azure" }}), element({{ value: "elevenlabs" }}), element({{ value: "mock" }})] }}),
      "#ttsVoice": element(),
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

    global.window = {{ location: {{ search: "", protocol: "http:", host: "testserver", origin: "http://testserver" }}, addEventListener() {{}}, CaptionState: {{ selectedLanguage: () => "all" }} }};
    global.document = {{
      querySelector(query) {{ return query === ".student-tts-panel" ? fakeTtsPanel : (nodes[query] || element()); }},
      querySelectorAll() {{ return []; }},
      createElement(tag) {{ return element({{ tagName: tag.toUpperCase() }}); }},
    }};
    global.URL.createObjectURL = () => "blob:tts";
    global.URL.revokeObjectURL = () => {{}};
    global.Audio = class {{ pause() {{}} removeAttribute() {{}} load() {{}} play() {{ return Promise.resolve(); }} }};
    global.fetch = async (url) => {{
      assert.strictEqual(String(url), "/api/tts/status");
      return {{ ok: true, json: async () => ({{
        enabled: true,
        active_provider: "azure",
        provider: "azure",
        ready: true,
        missing: [],
        providers: {{
          azure: {{ ready: true, status: "ready", voices: {{ kk: [
            {{ id: "kk-female", name: "Aigul", gender: "female", provider: "azure", language: "kk" }},
            {{ id: "kk-male", name: "Daulet", gender: "male", provider: "azure", language: "kk" }},
          ], uz: [{{ id: "uz-female", name: "Madina", gender: "female", provider: "azure", language: "uz" }}] }} }},
          elevenlabs: {{ ready: false, status: "not_configured", experimental: true, voices: {{ kk: [] }} }},
          mock: {{ ready: true, status: "ready", voices: {{ kk: [{{ id: "mock-kk-female", name: "Mock", gender: "female", provider: "mock", language: "kk" }}] }} }},
        }},
      }}) }};
    }};

    {captions_js}
    {tts_js}

    (async () => {{
      await new Promise((resolve) => setTimeout(resolve, 0));
      assert.deepStrictEqual(nodes["#ttsVoice"].options.map((option) => option.value), ["kk-female", "kk-male"]);
      assert.match(nodes["#ttsVoice"].options[0].textContent, /Aigul/);
      assert.match(nodes["#ttsVoiceStatus"].textContent, /2 Azure voices available for Kazakh|Using kk-female/);
      nodes["#ttsLanguage"].value = "uz";
      nodes["#ttsLanguage"].handlers.change({{ target: nodes["#ttsLanguage"] }});
      assert.deepStrictEqual(nodes["#ttsVoice"].options.map((option) => option.value), ["uz-female"]);
      nodes["#ttsProvider"].value = "elevenlabs";
      nodes["#ttsProvider"].handlers.change({{ target: nodes["#ttsProvider"] }});
      assert.strictEqual(nodes["#ttsVoice"].disabled, true);
      assert.match(nodes["#ttsVoiceStatus"].textContent, /No ElevenLabs voices/);
    }})();
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def test_student_tts_js_does_not_send_voice_gender():
    text = Path("app/web/static/student_tts.js").read_text(encoding="utf-8")

    assert "voice_gender" not in text
    assert "selectedVoiceGender" not in text
    assert "voiceGender" not in text


def test_student_tts_js_enables_tts_when_selected_provider_is_ready_even_if_active_provider_is_not_ready():
    if shutil.which("node") is None:
        pytest.skip("node is required for student_tts.js selected provider readiness test")

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
        innerHTML: "",
        handlers: {{}},
        appendChild(child) {{ this.options.push(child); }},
        addEventListener(event, handler) {{ this.handlers[event] = handler; }},
        ...overrides,
      }};
    }}

    const fakeTtsPanel = element({{ dataset: {{ lessonId: "lesson-tts", autoplayDefault: "true", queueMode: "sequential", volumeDefault: "1", duckingEnabled: "false" }} }});
    const nodes = {{
      "#ttsStatus": element(),
      "#ttsEnabled": element({{ checked: true }}),
      "#ttsAutoplay": element({{ checked: true }}),
      "#ttsLanguage": element({{ value: "kk" }}),
      "#ttsProvider": element({{ value: "mock", options: [element({{ value: "azure" }}), element({{ value: "mock" }})] }}),
      "#ttsVoice": element(),
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

    global.window = {{ location: {{ search: "", protocol: "http:", host: "testserver", origin: "http://testserver" }}, addEventListener() {{}}, CaptionState: {{ selectedLanguage: () => "kk" }} }};
    global.document = {{
      querySelector(query) {{ return query === ".student-tts-panel" ? fakeTtsPanel : (nodes[query] || element()); }},
      querySelectorAll() {{ return []; }},
      createElement(tag) {{ return element({{ tagName: tag.toUpperCase() }}); }},
    }};
    global.URL.createObjectURL = () => "blob:tts";
    global.URL.revokeObjectURL = () => {{}};
    global.Audio = class {{ pause() {{}} removeAttribute() {{}} load() {{}} play() {{ return Promise.resolve(); }} }};
    global.fetch = async (url) => {{
      if (String(url).endsWith("/api/tts/status")) {{
        return {{ ok: true, json: async () => ({{
          enabled: true,
          active_provider: "azure",
          provider: "azure",
          ready: false,
          missing: ["AZURE_TTS_KEY"],
          providers: {{
            azure: {{ ready: false, status: "not_configured", voices: {{ kk: [] }} }},
            mock: {{ ready: true, status: "ready", voices: {{ kk: [{{ id: "mock-kk-1", name: "Mock", provider: "mock", language: "kk" }}] }} }},
          }},
        }}) }};
      }}
      return {{ ok: true, headers: {{ get() {{ return "false"; }} }}, blob: async () => ({{}}) }};
    }};

    {captions_js}
    {tts_js}

    (async () => {{
      await new Promise((resolve) => setTimeout(resolve, 0));
      assert.strictEqual(nodes["#ttsEnabled"].disabled, false);
      assert.strictEqual(nodes["#ttsPlayLatest"].disabled, false);
      assert.deepStrictEqual(nodes["#ttsVoice"].options.map((option) => option.value), ["mock-kk-1"]);
    }})();
    """

    result = _run_node_script(script)

    assert result.returncode == 0, result.stderr


def _run_node_script(script: str):
    return subprocess.run(["node", "-"], input=textwrap.dedent(script), capture_output=True, text=True, encoding="utf-8", timeout=10)


def _app(tmp_path, monkeypatch, db_name: str, provider: str = "mock"):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TTS_PROVIDER", provider)
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage24b-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post("/api/lessons", json={"title": "Stage 24B", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock"})
    assert response.status_code == 201, response.text
    return response.json()
