import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_real_provider_e2e_guide_exists_and_covers_full_manual_flow():
    guide = (ROOT / "docs" / "REAL_PROVIDER_E2E_TEST.md").read_text(encoding="utf-8")

    required_phrases = [
        "Stage 26H",
        "Do not run a 1000-student real-provider load test",
        "Do not paste secrets",
        "STT: elevenlabs or azure",
        "Translator: azure",
        "TTS: azure first, mock fallback",
        "Zoom: real meeting",
        "python scripts/check_deployment_readiness.py",
        "python scripts/collect_e2e_snapshot.py",
        "python scripts/run_real_provider_smoke_test.py",
        "kk",
        "uz",
        "zh-Hans",
        "TTS URL mode",
        "audio_url",
        "text question",
        "voice question",
        "/api/metrics/runtime",
        "export transcript",
    ]
    for phrase in required_phrases:
        assert phrase in guide


def test_real_provider_e2e_guide_has_exact_pass_fail_checklist():
    guide = (ROOT / "docs" / "REAL_PROVIDER_E2E_TEST.md").read_text(encoding="utf-8")

    checklist_items = [
        "Zoom meeting created",
        "teacher mic chunks grow",
        "STT final captions appear",
        "translations appear",
        "no Russian fallback for kk/uz/zh-Hans",
        "TTS does not read backlog",
        "TTS cache hit after repeated request",
        "questions arrive",
        "metrics show no provider errors",
        "transcript/export works",
    ]
    for item in checklist_items:
        assert item in guide


def test_real_provider_report_template_captures_quality_latency_and_verdict_fields():
    template = (ROOT / "docs" / "REAL_PROVIDER_E2E_REPORT_TEMPLATE.md").read_text(encoding="utf-8")

    fields = [
        "date/time",
        "commit/version",
        "environment",
        "server mode",
        "provider config status",
        "lesson_id",
        "Zoom meeting id masked",
        "STT provider",
        "translator",
        "TTS provider/voice",
        "languages tested",
        "caption latency",
        "TTS latency",
        "translation quality notes",
        "STT quality notes",
        "errors",
        "screenshots optional",
        "smoke script report",
        "final verdict",
    ]
    for field in fields:
        assert field in template


def test_collect_e2e_snapshot_help_works():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "collect_e2e_snapshot.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--base-url" in result.stdout
    assert "--lesson-id" in result.stdout
    assert "/api/health/ready" in result.stdout


def test_collect_e2e_snapshot_imports_and_sanitizes_fake_responses(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from scripts import collect_e2e_snapshot

    def fake_fetch_json(url, timeout):
        if url.endswith("/api/health/ready"):
            return {"ok": True, "json": {"status": "ready", "api_key": "real-secret"}}
        if url.endswith("/api/metrics/runtime"):
            return {"ok": True, "json": {"provider_errors_total": 0}}
        if url.endswith("/api/providers/status"):
            return {"ok": True, "json": {"zoom": {"meeting_id": "12345678901", "join_url": "https://zoom.test/j/123?pwd=secret"}}}
        if url.endswith("/api/tts/status"):
            return {"ok": True, "json": {"provider": "azure", "access_token": "token-value"}}
        if url.endswith("/api/lessons/lesson-1/diagnostics"):
            return {"ok": False, "status": 404, "error": "HTTPError"}
        raise AssertionError(url)

    monkeypatch.setattr(collect_e2e_snapshot, "_fetch_json", fake_fetch_json)

    snapshot = collect_e2e_snapshot.collect_snapshot("http://example.test", lesson_id="lesson-1", timeout=1.0)
    encoded = json.dumps(snapshot, sort_keys=True)

    assert snapshot["base_url"] == "http://example.test"
    assert "health_ready" in snapshot["endpoints"]
    assert "lesson_diagnostics" in snapshot["endpoints"]
    assert "real-secret" not in encoded
    assert "token-value" not in encoded
    assert "pwd=secret" not in encoded
    assert "[redacted]" in encoded


def test_real_provider_smoke_help_works():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_real_provider_smoke_test.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--base-url" in result.stdout
    assert "--tts-provider" in result.stdout
    assert "--language" in result.stdout
    assert "real-provider smoke" in result.stdout.lower()


def test_real_provider_smoke_sanitizer_hides_tokens_and_secret_keys():
    sys.path.insert(0, str(ROOT))
    from scripts import run_real_provider_smoke_test as smoke

    payload = {
        "audio_url": "http://127.0.0.1:8000/api/lessons/lesson_1/tts/audio/a1?token=signed.secret&format=mp3",
        "authorization": "Bearer real-token",
        "nested": {"api_key": "provider-secret", "meeting_id": "12345678901"},
        "plain_url": "https://example.test/path?signature=abc123&ok=1",
    }

    sanitized = smoke.sanitize_for_report(payload)
    encoded = json.dumps(sanitized, sort_keys=True)

    assert "signed.secret" not in encoded
    assert "real-token" not in encoded
    assert "provider-secret" not in encoded
    assert "12345678901" not in encoded
    assert "abc123" not in encoded
    assert "token=<redacted>" in encoded
    assert "signature=<redacted>" in encoded


def test_real_provider_smoke_report_writer_creates_json_and_markdown(tmp_path):
    sys.path.insert(0, str(ROOT))
    from scripts import run_real_provider_smoke_test as smoke

    report = {
        "verdict": "MANUAL_REQUIRED",
        "base_url": "http://127.0.0.1:8000",
        "lesson_id": "lesson_123",
        "checks": [
            {"name": "health_ready", "status": "PASS", "message": "ready"},
            {"name": "teacher_mic_manual", "status": "MANUAL", "message": "operator step"},
        ],
        "tts": {
            "audio_url": "http://x/audio?token=secret-token",
            "audio_bytes": 128,
            "provider": "azure",
            "language": "kk",
        },
        "manual_steps": ["Open /teacher/lesson_123 and say a short phrase."],
    }

    paths = smoke.write_reports(report, tmp_path)
    json_payload = json.loads(paths.json_path.read_text(encoding="utf-8"))
    markdown = paths.markdown_path.read_text(encoding="utf-8")

    assert paths.json_path.name.startswith("real_provider_smoke_")
    assert paths.markdown_path.name.startswith("real_provider_smoke_")
    assert paths.json_path.exists()
    assert paths.markdown_path.exists()
    assert "secret-token" not in paths.json_path.read_text(encoding="utf-8")
    assert json_payload["tts"]["audio_url"] == "http://x/audio?token=<redacted>"
    assert "Real Provider Smoke Test" in markdown
    assert "Overall Verdict: MANUAL_REQUIRED" in markdown
    assert "teacher_mic_manual" in markdown


def test_real_provider_smoke_run_uses_fake_client_without_real_provider_calls(tmp_path):
    sys.path.insert(0, str(ROOT))
    from scripts import run_real_provider_smoke_test as smoke

    calls = []

    class FakeClient:
        def get_json(self, path):
            calls.append(("GET_JSON", path))
            if path == "/api/health/ready":
                return {"status": 200, "json": {"status": "ready"}}
            if path == "/api/providers/status?live=true":
                return {
                    "status": 200,
                    "json": {
                        "stt": {"azure": {"ready": True}},
                        "translation": {"azure": {"ready": True}},
                        "tts": {"azure": {"ready": True}},
                    },
                }
            if path == "/api/tts/status":
                return {
                    "status": 200,
                    "json": {
                        "enabled": True,
                        "ready": True,
                        "providers": {"azure": {"ready": True}},
                    },
                }
            if path == "/api/metrics/runtime":
                return {"status": 200, "json": {"provider_errors_total": 0}}
            if path == "/api/lessons/lesson_1/diagnostics":
                return {"status": 200, "json": {"captions": {"sent": 1}}}
            raise AssertionError(path)

        def post_json(self, path, payload):
            calls.append(("POST_JSON", path, payload))
            if path == "/api/lessons":
                return {"status": 201, "json": {"lesson_id": "lesson_1"}}
            if path == "/api/lessons/lesson_1/tts/synthesize":
                return {
                    "status": 200,
                    "json": {
                        "audio_url": "/api/lessons/lesson_1/tts/audio/audio_1?token=signed.secret",
                        "provider": "azure",
                        "language": "kk",
                    },
                }
            raise AssertionError(path)

        def get_bytes(self, url_or_path):
            calls.append(("GET_BYTES", url_or_path))
            return {"status": 200, "bytes": b"RIFFdata", "content_type": "audio/wav"}

    options = smoke.SmokeOptions(
        base_url="http://example.test",
        tts_provider="azure",
        language="kk",
        reports_dir=tmp_path,
        manual=False,
        timeout=1.0,
    )

    report = smoke.run_smoke_test(options, client=FakeClient())
    encoded = json.dumps(report, sort_keys=True)

    assert report["lesson_id"] == "lesson_1"
    assert report["tts"]["audio_bytes"] == len(b"RIFFdata")
    assert "signed.secret" not in encoded
    assert ("POST_JSON", "/api/lessons/lesson_1/tts/synthesize", {
        "text": "Hello from the real provider smoke test.",
        "language": "kk",
        "provider": "azure",
        "return_mode": "url",
    }) in calls
