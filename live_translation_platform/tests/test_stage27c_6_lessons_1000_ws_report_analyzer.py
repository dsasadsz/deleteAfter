import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/analyze_6_lessons_1000_ws_report.py"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _base_report(**overrides):
    lesson_results = [
        {
            "lesson_id": f"lesson_{index}",
            "students_connected": 167 if index < 4 else 166,
            "captions_published": 360,
            "total_received": (167 if index < 4 else 166) * 360,
            "receive_rate": 1.0,
            "p50_caption_latency_ms": 45,
            "p95_caption_latency_ms": 250,
            "p99_caption_latency_ms": 500,
            "disconnects": 0,
            "errors": 0,
        }
        for index in range(6)
    ]
    report = {
        "run_started_at_utc": "2026-05-24T00:00:00Z",
        "run_finished_at_utc": "2026-05-24T00:02:00Z",
        "base_url": "http://127.0.0.1:8000",
        "ws_base_url": "ws://127.0.0.1:8000",
        "lessons": 6,
        "students": 1000,
        "duration_seconds": 120,
        "captions_per_second": 3,
        "student_distribution": "even",
        "overall_result": "pass",
        "mock_only": True,
        "real_provider_proof": False,
        "per_lesson_results": lesson_results,
        "aggregate_results": {
            "students_requested": 1000,
            "students_connected": 1000,
            "captions_published": 2160,
            "total_received": 360000,
            "receive_rate": 1.0,
            "p50_caption_latency_ms": 45,
            "p95_caption_latency_ms": 250,
            "p99_caption_latency_ms": 500,
            "disconnects": 0,
            "errors": 0,
        },
        "runtime_metrics_before": {
            "websocket_send_timeouts_total": 0,
            "websocket_send_failures_total": 0,
            "redis_pubsub_messages_published_total": 0,
            "redis_pubsub_messages_received_total": 0,
            "redis_pubsub_errors_total": 0,
        },
        "runtime_metrics_during": [{"caption_ws_clients": 1000}],
        "runtime_metrics_after": {
            "caption_ws_clients": 0,
            "websocket_send_timeouts_total": 0,
            "websocket_send_failures_total": 0,
            "redis_pubsub_messages_published_total": 2160,
            "redis_pubsub_messages_received_total": 2160,
            "redis_pubsub_errors_total": 0,
        },
        "errors_sanitized": [],
        "thresholds": {"min_receive_rate": 0.99, "p95_caption_latency_ms": 1000},
    }
    report.update(overrides)
    return report


def _run_analyzer(report: dict, tmp_path: Path, *extra_args: str):
    report_path = tmp_path / "stage27b_report.json"
    output_path = tmp_path / "analysis.json"
    _write_json(report_path, report)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--report-json",
            str(report_path),
            "--output-json",
            str(output_path),
            *extra_args,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    return result, payload


def test_help_works():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--report-json" in result.stdout
    assert "--max-p95-latency-ms" in result.stdout
    assert "--max-ws-timeout-count" in result.stdout


def test_valid_passing_report_returns_pass(tmp_path):
    result, payload = _run_analyzer(_base_report(), tmp_path, "--require-final-result-pass")

    assert result.returncode == 0
    assert payload["verdict"] == "PASS"
    assert payload["mock_only"] is True
    assert payload["real_provider_proof"] is False
    assert payload["students"] == 1000
    assert payload["lessons"] == 6
    assert payload["connected_clients_total"] == 1000
    assert payload["receive_rate"] == 1.0
    assert payload["latency"]["p95_ms"] == 250
    assert len(payload["per_lesson_summary"]) == 6
    assert payload["failed_thresholds"] == []


def test_report_with_missing_fields_returns_invalid_report(tmp_path):
    result, payload = _run_analyzer({"overall_result": "pass", "mock_only": True}, tmp_path)

    assert result.returncode == 2
    assert payload["verdict"] == "INVALID_REPORT"
    assert payload["failed_thresholds"]


def test_low_receive_rate_returns_fail(tmp_path):
    report = _base_report()
    report["aggregate_results"]["receive_rate"] = 0.80
    result, payload = _run_analyzer(report, tmp_path, "--min-receive-rate", "0.99")

    assert result.returncode == 1
    assert payload["verdict"] == "FAIL"
    assert any(item["name"] == "receive_rate" for item in payload["failed_thresholds"])
    assert payload["bottleneck_hints"]


def test_high_p95_returns_fail(tmp_path):
    report = _base_report()
    report["aggregate_results"]["p95_caption_latency_ms"] = 1500
    result, payload = _run_analyzer(report, tmp_path, "--max-p95-latency-ms", "1000")

    assert result.returncode == 1
    assert payload["verdict"] == "FAIL"
    assert any(item["name"] == "p95_latency_ms" for item in payload["failed_thresholds"])


def test_warnings_produce_pass_with_warnings(tmp_path):
    report = _base_report(runtime_metrics_before={}, runtime_metrics_after={}, runtime_metrics_during=[])
    result, payload = _run_analyzer(report, tmp_path)

    assert result.returncode == 0
    assert payload["verdict"] == "PASS_WITH_WARNINGS"
    assert payload["failed_thresholds"] == []
    assert any("runtime metrics" in warning for warning in payload["warnings"])


def test_output_json_includes_required_analysis_fields(tmp_path):
    report = _base_report()
    report["runtime_metrics_after"]["websocket_send_timeouts_total"] = 1
    result, payload = _run_analyzer(report, tmp_path, "--max-ws-timeout-count", "0")

    assert result.returncode == 1
    for field in ("verdict", "failed_thresholds", "warnings", "bottleneck_hints", "scenario", "per_lesson_summary"):
        assert field in payload
    assert any("slow clients or proxy/network pressure" in hint for hint in payload["bottleneck_hints"])


def test_docs_mention_mock_only_and_real_provider_limitations():
    docs = "\n".join(
        [
            _read("docs/6-lessons-1000-ws-report-analysis.md"),
            _read("docs/6-lessons-1000-students-load-test.md"),
            _read("docs/load-testing.md"),
            _read("docs/production-evidence-validation.md"),
            _read("docs/release-operator-runbook.md"),
            _read("docs/architecture-audit-1000-users-6-lessons.md"),
        ]
    )

    assert "Stage 27B runs the load test" in docs
    assert "Stage 27C analyzes the report" in docs
    assert "PASS here proves mock WebSocket fanout only" in docs
    assert "does not prove real STT/translation/TTS capacity" in docs
    assert "real-provider proof remains manual and separate" in docs
