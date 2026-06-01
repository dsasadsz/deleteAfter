import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_real_provider_e2e_help_includes_report_json():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/real_provider_e2e_check.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--report-json" in result.stdout


def test_dry_run_report_can_be_written_without_real_provider_calls(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    calls = []

    def fake_fetch_json(url, *, timeout, method="GET", payload=None, headers=None):
        calls.append((method, url, payload, headers))
        if url.endswith("/api/health/ready"):
            return {"status": "ready", "config_missing": []}
        if url.endswith("/api/providers/status"):
            return {
                "stt": {"azure": {"ready": True, "status": "ready"}},
                "translation": {"azure": {"ready": True, "status": "ready"}},
                "tts": {"azure": {"ready": True, "status": "ready"}},
            }
        if url.endswith("/api/tts/status"):
            return {
                "enabled": True,
                "ready": True,
                "provider": "azure",
                "providers": {"azure": {"ready": True, "status": "ready"}},
                "audio_url_enabled": True,
            }
        raise AssertionError(url)

    monkeypatch.setattr(runner, "_fetch_json", fake_fetch_json)

    report_path = tmp_path / "real_provider_e2e_report.json"
    report = runner.run_check(
        runner.RunOptions(
            base_url="http://example.test",
            integration_key_env="INTEGRATION_KEY",
            dry_run=True,
            report_json=str(report_path),
            timeout=1.0,
        )
    )
    runner.write_report_json(report, report_path)

    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["run_mode"] == "dry_run"
    assert saved["base_url"] == "http://example.test"
    assert saved["checked_endpoints"] == ["/api/health/ready", "/api/providers/status", "/api/tts/status"]
    assert saved["tts_check_requested"] is False
    assert saved["tts_real_call_allowed"] is False
    assert saved["zoom_call_allowed"] is False
    assert saved["final_result"] == "pass"
    assert "status_summary" in saved
    assert "latency_ms" in saved
    assert report["final_result"] == "pass"
    assert [call[0] for call in calls] == ["GET", "GET", "GET"]


def test_report_sanitizes_secret_like_values(tmp_path):
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    report = {
        "run_mode": "dry_run",
        "timestamp_utc": "2026-05-23T00:00:00Z",
        "base_url": "http://example.test",
        "checked_endpoints": ["/api/health/ready"],
        "tts_check_requested": False,
        "tts_real_call_allowed": False,
        "zoom_call_allowed": False,
        "status_summary": {"authorization": "Bearer secret-token"},
        "latency_ms": {"/api/health/ready": 1.0},
        "sanitized_errors": ["https://example.test/audio?token=secret-token"],
        "final_result": "pass",
        "api_key": "provider-secret",
        "cookie": "session=secret",
    }

    report_path = tmp_path / "report.json"
    runner.write_report_json(report, report_path)
    encoded = report_path.read_text(encoding="utf-8")

    assert "secret-token" not in encoded
    assert "provider-secret" not in encoded
    assert "session=secret" not in encoded
    assert "token=<redacted>" in encoded
    assert '"final_result": "pass"' in encoded


def test_real_provider_e2e_report_docs_cover_modes_and_ci_guard():
    doc = _read("docs/real-provider-e2e-report.md")

    required = [
        "dry-run report",
        "preflight report",
        "real-provider report",
        "CI guard",
        "normal CI",
        "--report-json",
        "--allow-real-provider-calls",
        "--allow-zoom-call",
        "final_result",
        "sanitized",
    ]
    for phrase in required:
        assert phrase in doc

    production = _read("docs/production.md")
    load_testing = _read("docs/load-testing.md")
    for content in (production, load_testing):
        assert "docs/real-provider-e2e-report.md" in content
        assert "must not run automatically in normal CI" in content
        assert "--allow-real-provider-calls" in content


def test_report_contract_has_required_top_level_fields():
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    report = runner.build_execution_report(
        runner.RunOptions(base_url="http://example.test", dry_run=True),
        endpoint_results={
            "/api/health/ready": runner.EndpointResult(ok=True, latency_ms=1.2),
            "/api/providers/status": runner.EndpointResult(ok=True, latency_ms=2.3),
        },
        base_report={
            "health_ready": True,
            "warnings": [],
            "tts_check": {"ran": False, "success": False},
        },
    )

    required_keys = {
        "run_mode",
        "timestamp_utc",
        "base_url",
        "checked_endpoints",
        "tts_check_requested",
        "tts_real_call_allowed",
        "zoom_call_allowed",
        "status_summary",
        "latency_ms",
        "sanitized_errors",
        "final_result",
    }
    assert required_keys.issubset(report)
    assert report["final_result"] == "pass"
