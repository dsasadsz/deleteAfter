import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import analyze_6_lessons_1000_ws_report as analyzer
from scripts import load_test_6_lessons_1000_students as loadtest


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _failed_stage27b_report(**overrides):
    per_lesson = [
        {
            "lesson_id": f"lesson_{index}",
            "students_connected": 167 if index < 4 else 166,
            "captions_published": 360,
            "total_received": 59953 if index != 2 else 60120,
            "receive_rate": 0.997222 if index != 2 else 1.0,
            "p50_caption_latency_ms": 646.0,
            "p95_caption_latency_ms": 1037.56,
            "p99_caption_latency_ms": 1162.27,
            "disconnects": 0,
            "errors": 0,
        }
        for index in range(6)
    ]
    report = {
        "overall_result": "fail",
        "mock_only": True,
        "real_provider_proof": False,
        "lessons": 6,
        "students": 1000,
        "duration_seconds": 120,
        "captions_per_second": 3,
        "student_distribution": "even",
        "per_lesson_results": per_lesson,
        "aggregate_results": {
            "students_requested": 1000,
            "students_connected": 1000,
            "captions_published": 2160,
            "total_received": 359267,
            "receive_rate": 0.997964,
            "p50_caption_latency_ms": 646.70,
            "p95_caption_latency_ms": 1037.56,
            "p99_caption_latency_ms": 1162.27,
            "disconnects": 0,
            "errors": 0,
        },
        "runtime_metrics_before": {
            "caption_ws_clients": 0,
            "websocket_send_timeouts_total": 0,
            "websocket_send_failures_total": 0,
            "websocket_clients_dropped_total": 0,
            "redis_pubsub_messages_published_total": 0,
            "redis_pubsub_messages_received_total": 0,
            "redis_pubsub_errors_total": 0,
        },
        "runtime_metrics_during": [
            {
                "caption_ws_clients": 1000,
                "websocket_send_timeouts_total": 0,
                "websocket_send_failures_total": 0,
                "websocket_clients_dropped_total": 0,
                "redis_pubsub_messages_published_total": 2095,
                "redis_pubsub_messages_received_total": 2093,
                "redis_pubsub_errors_total": 0,
                "caption_broadcast_latency_ms_avg": 26.18,
                "redis_pubsub_latency_ms_avg": 52.5,
            }
        ],
        "runtime_metrics_after": {
            "caption_ws_clients": 0,
            "websocket_send_timeouts_total": 0,
            "websocket_send_failures_total": 124,
            "websocket_clients_dropped_total": 124,
            "redis_pubsub_messages_published_total": 2160,
            "redis_pubsub_messages_received_total": 2160,
            "redis_pubsub_errors_total": 0,
            "caption_broadcast_latency_ms_avg": 40.29,
            "redis_pubsub_latency_ms_avg": 103.41,
        },
        "errors_sanitized": [],
    }
    report.update(overrides)
    return report


def _args(**overrides):
    defaults = {
        "require_final_result_pass": False,
        "max_p95_latency_ms": 1000,
        "max_p99_latency_ms": 2000,
        "min_receive_rate": 0.99,
        "max_disconnect_rate": 0.01,
        "max_error_count": 0,
        "max_ws_timeout_count": 0,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_investigation_doc_exists_and_mentions_failed_run_values():
    doc = _read("docs/websocket-fanout-bottleneck-investigation.md")

    assert "1000 / 1000 clients connected" in doc
    assert "receive_rate = 0.997964" in doc
    assert "p95 = 1037.56ms" in doc
    assert "124 send failures/drops" in doc
    assert "Redis Pub/Sub published/received = 2160 / 2160" in doc
    assert "Redis Pub/Sub errors = 0" in doc


def test_analyzer_hints_send_failures_with_zero_timeouts():
    result = analyzer.analyze_report(_failed_stage27b_report(), _args())

    assert result["verdict"] == "FAIL"
    assert any("send failures increased while send timeouts stayed at 0" in hint for hint in result["bottleneck_hints"])
    assert any("shutdown" in hint.lower() for hint in result["bottleneck_hints"])


def test_analyzer_hints_redis_healthy_but_p95_high():
    result = analyzer.analyze_report(_failed_stage27b_report(), _args())

    assert any("Redis Pub/Sub counters look healthy" in hint for hint in result["bottleneck_hints"])
    assert any("WebSocket broadcast path" in hint or "client-side receive" in hint for hint in result["bottleneck_hints"])


def test_report_parsing_does_not_expose_secret_like_values():
    report = _failed_stage27b_report(
        base_url="http://127.0.0.1:8000?token=super-secret-token",
        ws_base_url="ws://127.0.0.1:8000/ws?token=super-secret-token",
        errors_sanitized=["Authorization: Bearer super-secret-token"],
    )

    result = analyzer.analyze_report(report, _args(max_error_count=10))
    encoded = json.dumps(result, sort_keys=True)

    assert "super-secret-token" not in encoded
    assert result["students"] == 1000


def test_docs_suggest_rerun_matrix():
    doc = _read("docs/websocket-fanout-bottleneck-investigation.md")

    assert "6 lessons / 1000 students / 120s / 3 cps baseline" in doc
    assert "6 lessons / 1000 students / 120s / 2 cps" in doc
    assert "6 lessons / 1000 students / 120s / 1 cps" in doc
    assert "6 lessons / 500 students / 120s / 3 cps" in doc
    assert "1 lesson / 1000 students / 120s / 3 cps" in doc
    assert "stronger host/Linux rerun" in doc
    assert "WEBSOCKET_BROADCAST_MAX_CONCURRENCY" in doc


def test_stage27b_build_report_includes_future_diagnostics_without_sockets():
    args = argparse.Namespace(
        base_url="http://127.0.0.1:8000",
        ws_base_url="ws://127.0.0.1:8000",
        lessons=1,
        students=1,
        duration_seconds=1,
        captions_per_second=1,
        student_distribution="even",
        assert_min_receive_rate=0.99,
        assert_p95_caption_latency_ms=1000,
        no_fail_on_thresholds=True,
    )
    report = loadtest.build_report(
        args=args,
        lesson_ids=["lesson_1"],
        clients=[
            loadtest.ClientStats(
                client_id=1,
                lesson_id="lesson_1",
                connected=True,
                received_count=1,
                latencies_ms=[10.0],
            )
        ],
        captions_published_by_lesson={"lesson_1": 1},
        runtime_metrics_before={"websocket_send_failures_total": 0, "websocket_clients_dropped_total": 0},
        runtime_metrics_during=[{"caption_ws_clients": 1, "websocket_send_failures_total": 0, "websocket_clients_dropped_total": 0}],
        runtime_metrics_after={"websocket_send_failures_total": 1, "websocket_clients_dropped_total": 1},
        errors=[],
        run_started_at_utc="2026-05-24T00:00:00Z",
        run_finished_at_utc="2026-05-24T00:00:02Z",
        diagnostics={
            "connection_ramp_up_seconds": 0.5,
            "runtime_metrics_before_shutdown": {"caption_ws_clients": 1, "websocket_send_failures_total": 0, "websocket_clients_dropped_total": 0},
            "publisher_request_latency_ms": {"p50": 5.0, "p95": 9.0, "p99": 10.0},
        },
    )

    assert report["diagnostics"]["peak_connected_clients"] == 1
    assert report["diagnostics"]["connection_ramp_up_seconds"] == 0.5
    assert report["diagnostics"]["publisher_request_latency_ms"]["p95"] == 9.0
    assert report["diagnostics"]["websocket_send_failures_total_delta_before_shutdown"] == 0.0
    assert report["diagnostics"]["websocket_send_failures_total_delta_after_shutdown"] == 1.0
