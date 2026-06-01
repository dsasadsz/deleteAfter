import re

from fastapi.testclient import TestClient

from app.main import create_app


def test_stage21_ducking_settings_defaults():
    from app.config import Settings

    settings = Settings()

    assert settings.tts_ducking_enabled is True
    assert settings.tts_ducking_level == 0.2
    assert settings.tts_ducking_restore_delay_ms == 300


def test_student_page_renders_ducking_controls(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, "stage21-ui.db")
    with TestClient(app) as client:
        lesson = _create_lesson(client)
        response = client.get(f"/student/{lesson['lesson_id']}")
    assert response.status_code == 200
    assert "data-ducking-enabled=\"true\"" in response.text
    assert "data-ducking-level=\"0.2\"" in response.text
    assert "data-ducking-restore-delay-ms=\"300\"" in response.text
    assert "ttsDuckingEnabled" in response.text
    assert "Original audio:" in response.text
    assert "Lower Zoom volume manually or mute original audio while TTS is playing." in response.text


def test_zoom_meeting_js_exposes_feature_detected_audio_ducking_adapter():
    text = open("app/web/static/zoom_meeting.js", encoding="utf-8").read()

    assert "window.ZoomAudioDucking" in text
    assert "function createZoomAudioDucking" in text
    assert "querySelectorAll(\"audio, video\")" in text
    assert "duck(level" in text
    assert "restore(delayMs" in text


def test_zoom_meeting_js_dispatches_ducking_status_on_window():
    text = open("app/web/static/zoom_meeting.js", encoding="utf-8").read()

    assert "window.dispatchEvent(new CustomEvent(\"zoom-audio-ducking-status\"" in text


def test_student_tts_js_integrates_duck_restore_state_machine():
    text = open("app/web/static/student_tts.js", encoding="utf-8").read()

    assert "duckingEnabled" in text
    assert "duckingLevel" in text
    assert "duckingRestoreDelayMs" in text
    assert "beginTtsDucking" in text
    assert "endTtsDucking" in text
    assert "ZoomAudioDucking?.duck" in text
    assert "ZoomAudioDucking?.restore" in text
    assert "Lower Zoom volume manually or mute original audio while TTS is playing." in text


def test_student_tts_js_forces_restore_before_disabling_ducking():
    text = open("app/web/static/student_tts.js", encoding="utf-8").read()

    assert "function endTtsDucking(force = false)" in text
    assert "if (!force && !ttsState.duckingEnabled) return;" in text
    assert re.search(
        r'ttsNodes\.duckingEnabled\?\.addEventListener\("change", \(event\) => \{\n'
        r'\s+const duckingEnabled = event\.target\.checked;\n'
        r'\s+if \(!duckingEnabled\) endTtsDucking\(true\);\n'
        r'\s+ttsState\.duckingEnabled = duckingEnabled;\n'
        r'\s+\}\);',
        text,
    )


def test_student_tts_js_ignores_stale_playback_callbacks():
    text = open("app/web/static/student_tts.js", encoding="utf-8").read()

    assert re.search(
        r'ttsState\.audio\.onended = \(\) => \{\n'
        r'\s+if \(playbackGeneration !== ttsState\.playbackGeneration\) return;',
        text,
    )
    assert re.search(
        r'ttsState\.audio\.onerror = \(\) => \{\n'
        r'\s+if \(playbackGeneration !== ttsState\.playbackGeneration\) return;',
        text,
    )
    assert re.search(
        r'\} catch \(error\) \{\n'
        r'\s+if \(playbackGeneration !== ttsState\.playbackGeneration\) \{\n'
        r'\s+if \(objectUrl && objectUrl !== ttsState\.currentObjectUrl\) URL\.revokeObjectURL\(objectUrl\);\n'
        r'\s+return;\n'
        r'\s+\}\n'
        r'\s+if \(objectUrl === ttsState\.currentObjectUrl\)',
        text,
    )


def test_stage21_docs_and_env_include_audio_ducking():
    env = open(".env.example", encoding="utf-8").read()
    project_report = open("docs/PROJECT_REPORT.md", encoding="utf-8").read()
    architecture = open("docs/ARCHITECTURE.md", encoding="utf-8").read()

    assert "TTS_DUCKING_ENABLED=true" in env
    assert "TTS_DUCKING_LEVEL=0.2" in env
    assert "TTS_DUCKING_RESTORE_DELAY_MS=300" in env
    assert "## 11. Audio Ducking" in project_report
    assert "mute original audio while TTS is playing" in project_report
    assert "Audio ducking is student-client-only" in architecture


def test_readme_keeps_browser_audio_settings_outside_stage21_section():
    readme = open("README.md", encoding="utf-8").read()

    assert "## Quick Start" in readme
    assert "## C# Integration Boundary" in readme
    assert "/api/lessons/*" in readme
    assert "not production API" in readme


def test_architecture_current_status_matches_stage21_scope():
    architecture = open("docs/ARCHITECTURE.md", encoding="utf-8").read()

    assert "after Stage 14" not in architecture
    assert "Stage 1-14" not in architecture
    assert "Current implemented status through Stage 22" in architecture


def test_architecture_real_zoom_mode_documents_browser_mic_default():
    architecture = open("docs/ARCHITECTURE.md", encoding="utf-8").read()
    real_zoom = architecture[
        architecture.index("### 5.2 Real Zoom Mode") : architecture.index("### 5.3 Smoke Test Flow")
    ]

    assert "teacher browser microphone" in real_zoom
    assert "RTMS remains experimental" in real_zoom
    assert "Python receives audio from Zoom RTMS server-side." not in real_zoom


def test_readme_current_flow_replaces_stage2_mock_only_wording():
    readme = open("README.md", encoding="utf-8").read()

    assert "Captions are still mock in both modes" not in readme
    assert "audio, STT, translation, captions, and video embed are still mock/demo until later stages" not in readme
    assert "C# must use only" in readme
    assert "audio_ingest_websocket_url" in readme
    assert "Meeting SDK embed config" in readme


def _app(tmp_path, monkeypatch, db_name: str):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / db_name).as_posix()}")
    monkeypatch.setenv("TTS_PROVIDER", "mock")
    monkeypatch.setenv("TTS_ENABLED", "true")
    monkeypatch.setenv("SECURITY_SIGNING_SECRET", "stage21-secret")
    monkeypatch.setenv("WEBSOCKET_AUTH_ENABLED", "false")
    monkeypatch.setenv("ALLOW_DEV_WS_WITHOUT_TOKEN", "true")
    return create_app()


def _create_lesson(client: TestClient) -> dict:
    response = client.post(
        "/api/lessons",
        json={"title": "Stage 21", "mode": "mock", "stt_provider": "mock", "translation_provider": "mock"},
    )
    assert response.status_code == 201, response.text
    return response.json()
