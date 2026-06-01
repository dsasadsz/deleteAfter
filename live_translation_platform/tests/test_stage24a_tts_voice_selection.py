import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def test_azure_tts_selects_explicit_voice_first():
    from app.tts.azure_tts import AzureTTS

    provider = AzureTTS(
        api_key="key",
        region="eastus",
        voices={"kk": "kk-default"},
        gender_voices={"kk": {"male": "kk-male", "female": "kk-female"}},
    )

    assert provider.resolve_voice("kk", explicit_voice="kk-explicit", voice_gender="female") == "kk-explicit"


def test_azure_tts_selects_configured_gender_voice():
    from app.tts.azure_tts import AzureTTS

    provider = AzureTTS(
        api_key="key",
        region="eastus",
        voices={"kk": "kk-default"},
        gender_voices={"kk": {"male": "kk-male", "female": "kk-female"}},
    )

    assert provider.resolve_voice("kk", voice_gender="male") == "kk-male"
    assert provider.resolve_voice("kk", voice_gender="female") == "kk-female"


def test_azure_tts_missing_gender_voice_falls_back_to_default():
    from app.tts.azure_tts import AzureTTS

    provider = AzureTTS(
        api_key="key",
        region="eastus",
        voices={"kk": "kk-default"},
        gender_voices={"kk": {"male": "", "female": ""}},
    )

    assert provider.resolve_voice("kk", voice_gender="male") == "kk-default"


def test_azure_tts_unsupported_language_has_clear_error():
    from app.tts.azure_tts import AzureTTS
    from app.tts.base import TTSConfigurationError

    provider = AzureTTS(api_key="key", region="eastus", voices={"kk": "kk-default"})

    with pytest.raises(TTSConfigurationError, match="Unsupported Azure TTS language"):
        provider.resolve_voice("en", voice_gender="male")


def test_tts_status_includes_grouped_voices_defaults_and_elevenlabs_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_TTS_KEY", "key")
    monkeypatch.setenv("AZURE_TTS_REGION", "eastus")
    monkeypatch.setenv("AZURE_TTS_DEFAULT_VOICE_KK", "kk-default")
    monkeypatch.setenv("AZURE_TTS_VOICE_KK_MALE", "kk-male")
    monkeypatch.setenv("AZURE_TTS_VOICE_KK_FEMALE", "kk-female")
    app = _app(tmp_path, monkeypatch, "stage24a-status.db", provider="azure")

    with TestClient(app) as client:
        response = client.get("/api/tts/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "azure"
    assert payload["ready"] is False
    assert payload["voices"]["kk"] == [
        {"id": "kk-default", "name": "kk-default", "short_name": "kk-default", "display_name": "kk-default", "gender": "unknown", "provider": "azure", "language": "kk", "locale": "kk-KZ", "experimental": False},
        {"id": "kk-male", "name": "kk-male", "short_name": "kk-male", "display_name": "kk-male", "gender": "male", "provider": "azure", "language": "kk", "locale": "kk-KZ", "experimental": False},
        {"id": "kk-female", "name": "kk-female", "short_name": "kk-female", "display_name": "kk-female", "gender": "female", "provider": "azure", "language": "kk", "locale": "kk-KZ", "experimental": False},
    ]
    assert payload["default_voice_by_language"]["kk"] == "kk-default"
    assert payload["selected_voice_support"]["provider_override"] is True
    assert payload["providers"]["azure"]["voices"]["kk"] == payload["voices"]["kk"]
    assert payload["selected_voice_support"]["providers"]["elevenlabs"]["status"] == "not_configured"
    assert payload["selected_voice_support"]["providers"]["elevenlabs"]["experimental"] is True


def test_mock_tts_status_returns_fixed_voice_catalog(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage24a-mock-status.db", provider="mock")

    with TestClient(app) as client:
        response = client.get("/api/tts/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert payload["default_voice_by_language"]["kk"] == "mock-kk-1"
    assert [voice["id"] for voice in payload["voices"]["kk"]] == ["mock-kk-1", "mock-kk-2"]


def test_tts_synthesize_accepts_voice_gender_but_uses_exact_or_default_voice(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage24a-synthesize.db", provider="mock")

    with TestClient(app) as client:
        lesson = _create_lesson(client)
        gender_response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"text": "Salem", "language": "kk", "voice_gender": "male"},
        )
        explicit_response = client.post(
            f"/api/lessons/{lesson['lesson_id']}/tts/synthesize",
            json={"text": "Salem", "language": "kk", "voice": "mock-kk-2", "voice_gender": "male"},
        )

    assert gender_response.status_code == 200, gender_response.text
    assert gender_response.headers["x-tts-voice"] == "mock-kk-1"
    assert explicit_response.status_code == 200, explicit_response.text
    assert explicit_response.headers["x-tts-voice"] == "mock-kk-2"


def _app(tmp_path, monkeypatch, db_name: str, provider: str = "mock"):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TTS_PROVIDER", provider)
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage24a-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post("/api/lessons", json={"title": "Stage 24A", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock"})
    assert response.status_code == 201, response.text
    return response.json()
