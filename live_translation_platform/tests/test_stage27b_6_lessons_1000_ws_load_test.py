import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/load_test_6_lessons_1000_students.py"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_help_works():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert "--lessons" in result.stdout
    assert "--students" in result.stdout
    assert "--allow-dev-load-test-endpoints" in result.stdout
    assert "--assert-p95-caption-latency-ms" in result.stdout


def test_student_distribution_across_6_lessons_is_even():
    sys.path.insert(0, str(ROOT))
    from scripts import load_test_6_lessons_1000_students as loadtest

    distribution = loadtest.distribute_students([f"lesson_{index}" for index in range(6)], 1000, "even")

    assert sum(distribution.values()) == 1000
    assert sorted(distribution.values()) == [166, 166, 167, 167, 167, 167]


def test_report_schema_can_be_generated_from_fake_inputs_without_sockets(tmp_path):
    sys.path.insert(0, str(ROOT))
    from scripts import load_test_6_lessons_1000_students as loadtest

    args = argparse.Namespace(
        base_url="http://127.0.0.1:8000?token=secret",
        ws_base_url="ws://127.0.0.1:8000",
        lessons=6,
        students=12,
        duration_seconds=1,
        captions_per_second=1,
        student_distribution="even",
        assert_min_receive_rate=0.99,
        assert_p95_caption_latency_ms=1000,
        no_fail_on_thresholds=False,
    )
    lesson_ids = [f"lesson_{index}" for index in range(6)]
    clients = []
    for index in range(12):
        clients.append(
            loadtest.ClientStats(
                client_id=index,
                lesson_id=lesson_ids[index % 6],
                connected=True,
                received_count=1,
                first_caption_latency_ms=20.0,
                last_caption_at="2026-05-24T00:00:01Z",
            )
        )

    report = loadtest.build_report(
        args=args,
        lesson_ids=lesson_ids,
        clients=clients,
        captions_published_by_lesson={lesson_id: 1 for lesson_id in lesson_ids},
        runtime_metrics_before={"websocket_send_failures_total": 0, "redis_pubsub_errors_total": 0},
        runtime_metrics_during=[{"caption_ws_clients": 12}],
        runtime_metrics_after={"websocket_send_failures_total": 0, "redis_pubsub_errors_total": 0},
        errors=[],
        run_started_at_utc="2026-05-24T00:00:00Z",
        run_finished_at_utc="2026-05-24T00:00:02Z",
    )

    assert report["mock_only"] is True
    assert report["real_provider_proof"] is False
    assert report["overall_result"] == "pass"
    assert report["lessons"] == 6
    assert report["students"] == 12
    assert len(report["per_lesson_results"]) == 6
    assert "secret" not in json.dumps(loadtest.sanitize_for_report(report))


def test_threshold_evaluation_returns_pass_fail_and_partial():
    sys.path.insert(0, str(ROOT))
    from scripts import load_test_6_lessons_1000_students as loadtest

    passing = {
        "students": 1000,
        "aggregate_results": {
            "students_connected": 1000,
            "receive_rate": 0.995,
            "p95_caption_latency_ms": 900,
            "disconnects": 0,
            "errors": 0,
        },
        "thresholds": {"min_receive_rate": 0.99, "p95_caption_latency_ms": 1000},
        "runtime_metrics_before": {
            "websocket_send_timeouts_total": 0,
            "websocket_send_failures_total": 0,
            "websocket_clients_dropped_total": 0,
            "redis_pubsub_errors_total": 0,
        },
        "runtime_metrics_after": {
            "websocket_send_timeouts_total": 0,
            "websocket_send_failures_total": 0,
            "websocket_clients_dropped_total": 0,
            "redis_pubsub_errors_total": 0,
        },
        "errors_sanitized": [],
    }
    failing = json.loads(json.dumps(passing))
    failing["aggregate_results"]["students_connected"] = 999
    failing["aggregate_results"]["receive_rate"] = 0.90
    failing["aggregate_results"]["p95_caption_latency_ms"] = 1200
    failing["runtime_metrics_after"]["redis_pubsub_errors_total"] = 1

    assert loadtest.evaluate_thresholds(passing, no_fail_on_thresholds=False).overall_result == "pass"
    failed = loadtest.evaluate_thresholds(failing, no_fail_on_thresholds=False)
    partial = loadtest.evaluate_thresholds(failing, no_fail_on_thresholds=True)

    assert failed.overall_result == "fail"
    assert partial.overall_result == "partial"
    assert any(check["name"] == "connected_students" for check in failed.checks)
    assert any(check["name"] == "redis_pubsub_errors_total_delta" for check in failed.checks)


def test_sanitization_removes_token_like_values():
    sys.path.insert(0, str(ROOT))
    from scripts import load_test_6_lessons_1000_students as loadtest

    sanitized = loadtest.sanitize_for_report(
        {
            "integration_key": "private-integration-key",
            "student_token": "private-student-token",
            "url": "ws://x/ws/lessons/lesson/captions?token=signed.secret&ok=1",
            "message": "Authorization: Bearer bearer-secret",
        }
    )
    encoded = json.dumps(sanitized)

    assert "private-integration-key" not in encoded
    assert "private-student-token" not in encoded
    assert "signed.secret" not in encoded
    assert "bearer-secret" not in encoded
    assert "<redacted>" in encoded or "[redacted]" in encoded


def test_mock_caption_payload_contains_latency_timestamp():
    sys.path.insert(0, str(ROOT))
    from scripts import load_test_6_lessons_1000_students as loadtest

    payload = loadtest.mock_caption_payload(42, "lesson_1", "2026-05-24T00:00:00Z")

    assert payload["sequence"] == 42
    assert payload["load_test_client_published_at"] == "2026-05-24T00:00:00Z"
    assert "Mock load-test caption" in payload["original_text"]


def test_docs_mention_mock_ws_load_is_not_real_provider_proof():
    docs = "\n".join(
        [
            _read("docs/6-lessons-1000-websocket-load-test.md"),
            _read("docs/load-testing.md"),
            _read("docs/1000-user-readiness-test.md"),
            _read("docs/architecture-audit-1000-users-6-lessons.md"),
            _read("docs/release-operator-runbook.md"),
            _read("docs/production-evidence-bundle.md"),
            _read("docs/production-evidence-validation.md"),
        ]
    )

    assert "mock WebSocket load is not real-provider proof" in docs
    assert "6 simultaneous lessons" in docs
    assert "1000 caption WebSocket clients" in docs
    assert "Redis Pub/Sub is required for multi-worker fanout" in docs
    assert "sticky routing or session redesign is still required for multi-worker teacher audio ingest" in docs
