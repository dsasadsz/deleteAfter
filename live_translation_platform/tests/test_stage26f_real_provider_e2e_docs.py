import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_real_provider_e2e_guide_exists_and_covers_safety_matrix():
    guide = _read("docs/real-provider-e2e.md")

    required_phrases = [
        "# Real Provider E2E Test Guide",
        "Mock load tests validate app architecture",
        "Zoom Server-to-Server OAuth",
        "Zoom Meeting SDK",
        "STT provider key: ElevenLabs/Azure/Cartesia",
        "Azure Translator",
        "TTS provider: Azure/ElevenLabs",
        "Test 1 - Provider readiness",
        "Test 2 - Real microphone -> STT -> translation -> captions",
        "Сегодня мы изучим C#, SQL, ASP.NET и Entity Framework.",
        "Test 3 - Real TTS URL mode",
        "first request cache miss",
        "second request cache hit",
        "no integration_key in browser",
        "Test 4 - Student questions text/voice",
        "Test 5 - Small-scale real provider load",
        "Do NOT test 1000 real users",
        "1 lesson",
        "5-10 simulated caption clients",
        "TTS enabled for 1-3 students only",
        "Test 6 - Provider quota/rate limit observation",
        "provider_errors_total",
        "stt_disconnects_total",
        "Do not commit .env",
        "Do not expose integration_key in browser",
    ]
    for phrase in required_phrases:
        assert phrase in guide


def test_real_provider_e2e_script_help_works():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/real_provider_e2e_check.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--base-url" in result.stdout
    assert "--integration-key-env" in result.stdout
    assert "--run-tts-check" in result.stdout
    assert "--allow-real-provider-calls" in result.stdout
    assert "--allow-zoom-call" in result.stdout


def test_real_provider_e2e_dry_run_only_calls_safe_status_endpoints(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    calls = []

    def fake_fetch_json(url, *, timeout, method="GET", payload=None, headers=None):
        calls.append((method, url, payload, headers))
        if url.endswith("/api/health/ready"):
            return {"status": "ready", "database_status": "ok"}
        if url.endswith("/api/providers/status"):
            return {"stt": {"azure": {"ready": True}}, "translation": {"azure": {"ready": True}}}
        if url.endswith("/api/tts/status"):
            return {"enabled": True, "ready": True, "provider": "azure", "providers": {"azure": {"ready": True}}}
        raise AssertionError(url)

    monkeypatch.setattr(runner, "_fetch_json", fake_fetch_json)

    report = runner.run_check(
        runner.RunOptions(
            base_url="http://example.test",
            integration_key_env="INTEGRATION_KEY",
            run_tts_check=False,
            allow_real_provider_calls=False,
            dry_run=True,
            timeout=1.0,
        )
    )

    assert report["health_ready"] is True
    assert report["lesson_created"] is False
    assert report["tts_check"]["ran"] is False
    assert [call[0] for call in calls] == ["GET", "GET", "GET"]
    assert [call[1].removeprefix("http://example.test") for call in calls] == [
        "/api/health/ready",
        "/api/providers/status",
        "/api/tts/status",
    ]


def test_real_provider_e2e_tts_check_requires_explicit_real_call_flag(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    def fake_fetch_json(url, *, timeout, method="GET", payload=None, headers=None):
        if url.endswith("/api/health/ready"):
            return {"status": "ready"}
        if url.endswith("/api/providers/status"):
            return {"stt": {"azure": {"ready": True}}}
        if url.endswith("/api/tts/status"):
            return {"enabled": True, "ready": True, "providers": {"azure": {"ready": True}}}
        raise AssertionError("TTS endpoint should not be called without allow flag")

    monkeypatch.setattr(runner, "_fetch_json", fake_fetch_json)

    report = runner.run_check(
        runner.RunOptions(
            base_url="http://example.test",
            integration_key_env="INTEGRATION_KEY",
            run_tts_check=True,
            allow_real_provider_calls=False,
            dry_run=False,
            lesson_id="lesson_1",
            timeout=1.0,
        )
    )

    assert report["tts_check"]["ran"] is False
    assert any("--allow-real-provider-calls" in warning for warning in report["warnings"])


def test_real_provider_e2e_sanitizer_hides_secrets_and_tokens():
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    payload = {
        "integration_key": "dev-secret",
        "audio_url": "https://example.test/audio?token=signed.secret&ok=1",
        "nested": {"api_key": "provider-secret", "authorization": "Bearer token"},
    }

    sanitized = runner.sanitize_for_report(payload)
    encoded = json.dumps(sanitized, sort_keys=True)

    assert "dev-secret" not in encoded
    assert "signed.secret" not in encoded
    assert "provider-secret" not in encoded
    assert "Bearer token" not in encoded
    assert "token=<redacted>" in encoded


def test_real_provider_e2e_docs_are_linked_from_load_testing_production_and_report():
    for path in ("docs/load-testing.md", "docs/production.md", "docs/PROJECT_REPORT.md", "README.md"):
        assert "docs/real-provider-e2e.md" in _read(path)


def test_real_provider_e2e_pytest_coverage_does_not_embed_real_provider_calls():
    test_source = _read("tests/test_stage26f_real_provider_e2e_docs.py")

    disallowed = [
        "api." + "elevenlabs.io",
        "cognitive" + "services",
        "zoom" + ".us",
        "AZURE" + "_",
        "ELEVENLABS" + "_",
    ]
    for fragment in disallowed:
        assert fragment not in test_source
