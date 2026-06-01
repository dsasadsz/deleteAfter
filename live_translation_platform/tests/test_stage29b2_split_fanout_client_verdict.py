import json
import subprocess
import sys
from pathlib import Path

from scripts import analyze_6_lessons_1000_ws_report as analyzer


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_production_evidence_bundle.py"


def _args(**overrides):
    defaults = {
        "require_final_result_pass": False,
        "max_p95_latency_ms": 1000,
        "max_p99_latency_ms": 2000,
        "min_receive_rate": 0.99,
        "max_disconnect_rate": 0.01,
        "max_error_count": 0,
        "max_ws_timeout_count": 0,
        "max_server_broadcast_p95_ms": 100,
        "max_redis_pubsub_p95_ms": 150,
        "allow_backend_pass_client_fail": False,
    }
    defaults.update(overrides)
    return type("Args", (), defaults)()


def _healthy_backend_client_p95_fail_report(**overrides):
    report = {
        "overall_result": "fail",
        "mock_only": True,
        "real_provider_proof": False,
        "lessons": 6,
        "students": 480,
        "aggregate_results": {
            "students_connected": 480,
            "receive_rate": 1.0,
            "p50_caption_latency_ms": 705.88,
            "p95_caption_latency_ms": 1090.54,
            "p99_caption_latency_ms": 1136.99,
            "disconnects": 0,
            "errors": 0,
        },
        "per_lesson_results": [
            {"lesson_id": f"lesson_{index}", "students_connected": 80, "receive_rate": 1.0}
            for index in range(6)
        ],
        "runtime_metrics_before": {
            "websocket_send_timeouts_total": 0,
            "websocket_send_failures_total": 0,
            "websocket_clients_dropped_total": 0,
            "redis_pubsub_messages_published_total": 0,
            "redis_pubsub_messages_received_total": 0,
            "redis_pubsub_errors_total": 0,
            "provider_timeout_errors_total": 0,
            "provider_rate_limit_errors_total": 0,
            "provider_auth_errors_total": 0,
            "provider_unknown_errors_total": 0,
        },
        "runtime_metrics_after": {
            "websocket_send_timeouts_total": 0,
            "websocket_send_failures_total": 0,
            "websocket_clients_dropped_total": 0,
            "redis_pubsub_messages_published_total": 1086,
            "redis_pubsub_messages_received_total": 1086,
            "redis_pubsub_errors_total": 0,
            "caption_broadcast_latency_ms_p95": 17.77,
            "redis_pubsub_latency_ms_p95": 67.45,
            "provider_timeout_errors_total": 0,
            "provider_rate_limit_errors_total": 0,
            "provider_auth_errors_total": 0,
            "provider_unknown_errors_total": 0,
        },
        "diagnostics": {
            "websocket_send_timeouts_total_delta_before_shutdown": 0,
            "websocket_send_failures_total_delta_before_shutdown": 0,
            "websocket_clients_dropped_total_delta_before_shutdown": 0,
        },
        "runtime_metrics_during": [{"caption_ws_clients": 480}],
        "errors_sanitized": [],
    }
    report.update(overrides)
    return report


def test_analyzer_help_includes_split_verdict_flags():
    result = subprocess.run(
        [sys.executable, "scripts/analyze_6_lessons_1000_ws_report.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--max-server-broadcast-p95-ms" in result.stdout
    assert "--max-redis-pubsub-p95-ms" in result.stdout
    assert "--allow-backend-pass-client-fail" in result.stdout


def test_healthy_backend_high_client_p95_splits_verdict_but_strict_overall_fails():
    result = analyzer.analyze_report(_healthy_backend_client_p95_fail_report(), _args())

    assert result["backend_fanout_verdict"] == "PASS"
    assert result["client_receive_verdict"] == "FAIL"
    assert result["overall_verdict"] == "FAIL"
    assert result["verdict"] == "FAIL"
    assert result["fanout_health"]["connected_clients_ok"] is True
    assert result["fanout_health"]["server_broadcast_p95_ms"] == 17.77
    assert result["fanout_health"]["redis_pubsub_p95_ms"] == 67.45
    assert result["fanout_health"]["redis_publish_receive_match"] is True
    assert result["client_receive_health"]["client_receive_p95_ms"] == 1090.54
    assert any("Backend fanout metrics are healthy while client receive p95 is high" in hint for hint in result["bottleneck_hints"])


def test_allow_backend_pass_client_fail_makes_overall_inconclusive():
    result = analyzer.analyze_report(
        _healthy_backend_client_p95_fail_report(),
        _args(allow_backend_pass_client_fail=True),
    )

    assert result["backend_fanout_verdict"] == "PASS"
    assert result["client_receive_verdict"] == "FAIL"
    assert result["overall_verdict"] == "INCONCLUSIVE"
    assert result["verdict"] == "INCONCLUSIVE"


def test_high_server_broadcast_p95_fails_backend_fanout():
    report = _healthy_backend_client_p95_fail_report()
    report["runtime_metrics_after"]["caption_broadcast_latency_ms_p95"] = 180

    result = analyzer.analyze_report(report, _args(allow_backend_pass_client_fail=True))

    assert result["backend_fanout_verdict"] == "FAIL"
    assert result["fanout_health"]["server_broadcast_p95_ms"] == 180
    assert result["overall_verdict"] == "FAIL"
    assert any("Server broadcast p95 is high" in hint for hint in result["bottleneck_hints"])


def test_redis_publish_receive_mismatch_fails_backend_fanout():
    report = _healthy_backend_client_p95_fail_report()
    report["runtime_metrics_after"]["redis_pubsub_messages_received_total"] = 1000

    result = analyzer.analyze_report(report, _args(allow_backend_pass_client_fail=True))

    assert result["backend_fanout_verdict"] == "FAIL"
    assert result["fanout_health"]["redis_publish_receive_match"] is False
    assert result["overall_verdict"] == "FAIL"


def test_after_shutdown_drops_only_warns_without_backend_fail():
    report = _healthy_backend_client_p95_fail_report(
        overall_result="pass",
        aggregate_results={
            "students_connected": 480,
            "receive_rate": 1.0,
            "p50_caption_latency_ms": 500,
            "p95_caption_latency_ms": 800,
            "p99_caption_latency_ms": 900,
            "disconnects": 0,
            "errors": 0,
        },
    )
    report["runtime_metrics_after"]["websocket_clients_dropped_total"] = 12
    report["diagnostics"]["websocket_clients_dropped_total_delta_before_shutdown"] = 0
    report["diagnostics"]["websocket_clients_dropped_total_delta_after_shutdown"] = 12

    result = analyzer.analyze_report(report, _args())

    assert result["backend_fanout_verdict"] == "PASS_WITH_WARNINGS"
    assert result["client_receive_verdict"] == "PASS"
    assert result["overall_verdict"] == "PASS_WITH_WARNINGS"
    assert any("Drops appeared after shutdown only" in hint for hint in result["bottleneck_hints"])


def test_validator_strict_proof_rejects_backend_only_inconclusive(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write_json(bundle / "manifest.json", {"sanitized": True, "final_bundle_status": "complete", "safety_notes": []})
    _write_json(bundle / "environment_summary.json", {"no_network": True})
    _write_json(bundle / "load_test_6_lessons_1000_ws_report.json", _healthy_backend_client_p95_fail_report())
    _write_json(
        bundle / "load_test_6_lessons_1000_ws_analysis.json",
        {
            "verdict": "INCONCLUSIVE",
            "overall_verdict": "INCONCLUSIVE",
            "backend_fanout_verdict": "PASS",
            "client_receive_verdict": "FAIL",
            "mock_only": True,
            "real_provider_proof": False,
            "students": 480,
            "lessons": 6,
            "connected_clients_total": 480,
            "failed_thresholds": [{"name": "p95_latency_ms"}],
            "warnings": [],
        },
    )

    result = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--bundle-dir",
            str(bundle),
            "--require-realistic-ws-pass",
            "--allow-ws-backend-only-inconclusive",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "NOT_READY" in result.stdout
    assert "backend_fanout_pass_client_inconclusive" in result.stdout


def test_docs_describe_backend_fanout_vs_client_receive_split():
    docs = "\n".join(
        [
            _read("docs/stage29b-observability-rerun-result.md"),
            _read("docs/websocket-fanout-bottleneck-investigation.md"),
            _read("docs/6-lessons-1000-ws-report-analysis.md"),
            _read("docs/production-evidence-validation.md"),
            _read("docs/load-testing.md"),
            _read("docs/staging-1000-ws-test-runbook.md"),
        ]
    )

    assert "backend fanout" in docs.lower()
    assert "client receive" in docs.lower()
    assert "backend-only" in docs.lower()
    assert "strict evidence still requires overall" in docs.lower()


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")
