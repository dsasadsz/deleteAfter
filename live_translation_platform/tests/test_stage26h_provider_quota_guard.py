import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _fake_status_fetch(calls):
    def fake_fetch_json(url, *, timeout, method="GET", payload=None, headers=None):
        calls.append((method, url, payload, headers))
        if url.endswith("/api/health/ready"):
            return {"status": "ready"}
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
            }
        if url.endswith("/tts/synthesize"):
            return {"audio_url": "/audio?token=real.secret", "provider": "azure", "language": "kk"}
        raise AssertionError(url)

    return fake_fetch_json


def test_help_includes_provider_quota_guard_flags():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/real_provider_e2e_check.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    for flag in (
        "--max-tts-calls",
        "--max-zoom-meetings",
        "--max-total-provider-calls",
        "--latency-threshold-ms",
        "--require-quota-confirmation",
        "--quota-confirmed",
    ):
        assert flag in result.stdout


def test_dry_run_does_not_require_quota_confirmation(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    calls = []
    monkeypatch.setattr(runner, "_fetch_json", _fake_status_fetch(calls))

    report = runner.run_check(
        runner.RunOptions(
            base_url="http://example.test",
            dry_run=True,
            require_quota_confirmation=True,
            timeout=1.0,
        )
    )

    assert report["run_mode"] == "dry_run"
    assert report["quota_guard_enabled"] is True
    assert report["quota_confirmed"] is False
    assert report["quota_guard_result"] == "not_required"
    assert report["final_result"] == "pass"
    assert [call[0] for call in calls] == ["GET", "GET", "GET"]


def test_real_provider_tts_requires_quota_confirmation_before_provider_call(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    calls = []
    monkeypatch.setenv("INTEGRATION_KEY", "secret-key")
    monkeypatch.setattr(runner, "_fetch_json", _fake_status_fetch(calls))

    report = runner.run_check(
        runner.RunOptions(
            base_url="http://example.test",
            dry_run=False,
            run_tts_check=True,
            allow_real_provider_calls=True,
            require_quota_confirmation=True,
            quota_confirmed=False,
            lesson_id="lesson_1",
            timeout=1.0,
        )
    )

    assert report["planned_provider_calls"]["tts_calls"] == 1
    assert report["quota_guard_result"] == "fail"
    assert report["tts_check"]["ran"] is False
    assert report["final_result"] == "fail"
    assert not any(call[1].endswith("/tts/synthesize") for call in calls)


def test_max_tts_calls_zero_blocks_requested_tts_check(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    calls = []
    monkeypatch.setenv("INTEGRATION_KEY", "secret-key")
    monkeypatch.setattr(runner, "_fetch_json", _fake_status_fetch(calls))

    report = runner.run_check(
        runner.RunOptions(
            base_url="http://example.test",
            dry_run=False,
            run_tts_check=True,
            allow_real_provider_calls=True,
            quota_confirmed=True,
            max_tts_calls=0,
            lesson_id="lesson_1",
            timeout=1.0,
        )
    )

    assert report["max_tts_calls"] == 0
    assert report["quota_guard_result"] == "fail"
    assert report["planned_provider_calls"]["tts_calls"] == 1
    assert report["tts_check"]["ran"] is False
    assert not any(call[1].endswith("/tts/synthesize") for call in calls)


def test_report_includes_quota_guard_and_latency_threshold_fields(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    calls = []
    monkeypatch.setattr(runner, "_fetch_json", _fake_status_fetch(calls))
    monkeypatch.setattr(
        runner,
        "_timed_fetch_json",
        lambda base_url, path, timeout, results: results.setdefault(path, runner.EndpointResult(ok=True, latency_ms=25.0))
        or {"status": "ready"} if path.endswith("ready") else {"enabled": True, "ready": True},
    )

    report = runner.run_check(
        runner.RunOptions(
            base_url="http://example.test",
            dry_run=True,
            latency_threshold_ms=10,
            timeout=1.0,
        )
    )

    assert "quota_guard_result" in report
    assert "planned_provider_calls" in report
    assert report["latency_threshold_ms"] == 10
    assert report["latency_threshold_violations"]
    assert report["final_result"] == "fail"


def test_quota_guard_report_sanitizes_secret_like_values(tmp_path):
    sys.path.insert(0, str(ROOT))
    from scripts import real_provider_e2e_check as runner

    report = {
        "quota_guard_result": "fail",
        "planned_provider_calls": {"tts_calls": 1},
        "sanitized_errors": [
            "blocked https://example.test/path?token=real.secret",
            "Authorization: Bearer very-secret",
        ],
        "integration_key": "secret-key",
        "cookie": "session=secret",
        "final_result": "fail",
    }
    path = tmp_path / "quota.json"
    runner.write_report_json(report, path)
    encoded = path.read_text(encoding="utf-8")

    assert "real.secret" not in encoded
    assert "very-secret" not in encoded
    assert "secret-key" not in encoded
    assert "session=secret" not in encoded
    assert "token=<redacted>" in encoded
    assert '"quota_guard_result": "fail"' in encoded


def test_report_docs_describe_quota_guard_and_billing_boundary():
    report_doc = _read("docs/real-provider-e2e-report.md")
    production = _read("docs/production.md")
    load_testing = _read("docs/load-testing.md")

    for phrase in (
        "quota guard",
        "quota_guard_result",
        "--require-quota-confirmation",
        "--quota-confirmed",
        "not a provider billing check",
    ):
        assert phrase in report_doc
    for content in (production, load_testing):
        assert "quota guard" in content
        assert "normal CI must not include quota-confirmed real-provider calls" in content
