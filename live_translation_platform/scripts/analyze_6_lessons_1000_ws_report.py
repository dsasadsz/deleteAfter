#!/usr/bin/env python
"""Analyze a Stage 27B 6-lessons / 1000 WebSocket mock load report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PASS = "PASS"
PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
INCONCLUSIVE = "INCONCLUSIVE"
INVALID_REPORT = "INVALID_REPORT"

DEFAULT_ANALYZER_ARGS = {
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read an existing Stage 27B 6-lessons/1000-WebSocket JSON report and produce "
            "a read-only verdict. This analyzer does not open sockets, call providers, or run load tests."
        )
    )
    parser.add_argument("--report-json", default="tmp/load_test_6_lessons_1000_students_report.json", help="Stage 27B report JSON path.")
    parser.add_argument(
        "--output-json",
        default="tmp/load_test_6_lessons_1000_students_analysis.json",
        help="Where to write analyzer JSON output.",
    )
    parser.add_argument("--require-final-result-pass", action="store_true", help="Require Stage 27B final/overall result to be pass.")
    parser.add_argument("--max-p95-latency-ms", type=float, default=1000, help="Maximum allowed aggregate p95 caption latency.")
    parser.add_argument("--max-p99-latency-ms", type=float, default=2000, help="Maximum allowed aggregate p99 caption latency.")
    parser.add_argument("--min-receive-rate", type=float, default=0.99, help="Minimum aggregate receive rate.")
    parser.add_argument("--max-disconnect-rate", type=float, default=0.01, help="Maximum disconnects divided by requested students.")
    parser.add_argument("--max-error-count", type=int, default=0, help="Maximum aggregate report/client error count.")
    parser.add_argument("--max-ws-timeout-count", type=int, default=0, help="Maximum WebSocket send timeout delta during the run.")
    parser.add_argument("--max-server-broadcast-p95-ms", type=float, default=100, help="Maximum server-side caption broadcast p95 latency.")
    parser.add_argument("--max-redis-pubsub-p95-ms", type=float, default=150, help="Maximum Redis Pub/Sub p95 latency.")
    parser.add_argument(
        "--allow-backend-pass-client-fail",
        action="store_true",
        help="Return INCONCLUSIVE instead of FAIL when backend fanout is healthy but client receive latency alone fails.",
    )
    return parser


def analyze_report(report: Any, args: argparse.Namespace) -> dict[str, Any]:
    args = normalize_args(args)
    invalid_reasons = validate_report(report)
    if invalid_reasons:
        return invalid_analysis(invalid_reasons)

    aggregate = report["aggregate_results"]
    per_lesson = report["per_lesson_results"]
    runtime_before = _dict(report.get("runtime_metrics_before"))
    runtime_after = _dict(report.get("runtime_metrics_after"))
    runtime_during = report.get("runtime_metrics_during") if isinstance(report.get("runtime_metrics_during"), list) else []

    students = _int(report.get("students"))
    lessons = _int(report.get("lessons"))
    final_result = effective_final_result(report)
    connected_clients_total = _int(aggregate.get("students_connected"))
    receive_rate = _float(aggregate.get("receive_rate"))
    latency = {
        "p50_ms": _number_or_none(aggregate.get("p50_caption_latency_ms")),
        "p95_ms": _number_or_none(aggregate.get("p95_caption_latency_ms")),
        "p99_ms": _number_or_none(aggregate.get("p99_caption_latency_ms")),
    }
    disconnects = _int(aggregate.get("disconnects"))
    error_count = _int(aggregate.get("errors")) + len(report.get("errors_sanitized") or [])
    ws_timeout_delta = metric_delta(runtime_before, runtime_after, "websocket_send_timeouts_total")
    ws_failure_delta = metric_delta(runtime_before, runtime_after, "websocket_send_failures_total")
    ws_drop_delta = metric_delta(runtime_before, runtime_after, "websocket_clients_dropped_total")
    fanout_health = build_fanout_health(
        report=report,
        runtime_before=runtime_before,
        runtime_after=runtime_after,
        connected_clients_total=connected_clients_total,
        students=students,
        receive_rate=receive_rate,
        args=args,
    )
    client_receive_health = build_client_receive_health(
        latency=latency,
        receive_rate=receive_rate,
        disconnects=disconnects,
        error_count=error_count,
        args=args,
    )
    backend_fanout_verdict = choose_backend_fanout_verdict(fanout_health)
    client_receive_verdict = choose_client_receive_verdict(client_receive_health)
    failed_thresholds = evaluate_thresholds(
        args=args,
        report=report,
        final_result=final_result,
        connected_clients_total=connected_clients_total,
        receive_rate=receive_rate,
        latency=latency,
        disconnects=disconnects,
        error_count=error_count,
        ws_timeout_delta=ws_timeout_delta,
        ws_failure_delta=ws_failure_delta,
        ws_drop_delta=ws_drop_delta,
        per_lesson=per_lesson,
    )
    warnings = build_warnings(
        report=report,
        per_lesson=per_lesson,
        runtime_before=runtime_before,
        runtime_after=runtime_after,
        runtime_during=runtime_during,
        disconnects=disconnects,
        students=students,
        latency=latency,
        args=args,
    )
    warnings.extend(fanout_health.get("warnings", []))
    hints = bottleneck_hints(
        report=report,
        per_lesson=per_lesson,
        runtime_before=runtime_before,
        runtime_after=runtime_after,
        fanout_health=fanout_health,
        backend_fanout_verdict=backend_fanout_verdict,
        client_receive_verdict=client_receive_verdict,
        connected_clients_total=connected_clients_total,
        students=students,
        receive_rate=receive_rate,
        latency=latency,
        disconnects=disconnects,
        error_count=error_count,
        ws_timeout_delta=ws_timeout_delta,
        ws_failure_delta=ws_failure_delta,
        ws_drop_delta=ws_drop_delta,
        args=args,
    )
    overall_verdict = choose_overall_verdict(
        backend_fanout_verdict=backend_fanout_verdict,
        client_receive_verdict=client_receive_verdict,
        client_receive_health=client_receive_health,
        args=args,
        warnings=warnings,
    )
    verdict = overall_verdict
    return {
        "verdict": verdict,
        "overall_verdict": overall_verdict,
        "backend_fanout_verdict": backend_fanout_verdict,
        "client_receive_verdict": client_receive_verdict,
        "fanout_health": fanout_health,
        "client_receive_health": client_receive_health,
        "scenario": scenario(report),
        "final_result": final_result,
        "mock_only": report.get("mock_only"),
        "real_provider_proof": report.get("real_provider_proof"),
        "students": students,
        "lessons": lessons,
        "connected_clients_total": connected_clients_total,
        "receive_rate": receive_rate,
        "latency": latency,
        "disconnects": disconnects,
        "errors_count": error_count,
        "ws_timeout_count": ws_timeout_delta,
        "ws_send_failures_count": ws_failure_delta,
        "ws_clients_dropped_count": ws_drop_delta,
        "per_lesson_summary": per_lesson_summary(per_lesson),
        "failed_thresholds": failed_thresholds,
        "warnings": warnings,
        "bottleneck_hints": hints,
        "mock_websocket_fanout_only": True,
        "real_provider_capacity_proven": False,
    }


def validate_report(report: Any) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return [_failure("report", None, "report JSON root must be an object")]
    missing = [
        name
        for name in (
            "mock_only",
            "real_provider_proof",
            "students",
            "lessons",
            "aggregate_results",
            "per_lesson_results",
        )
        if name not in report
    ]
    failures = [_failure("required_field", name, f"missing required field: {name}") for name in missing]
    if report.get("mock_only") is not True:
        failures.append(_failure("mock_only", report.get("mock_only"), "Stage 27B report must have mock_only=true"))
    if report.get("real_provider_proof") is not False:
        failures.append(_failure("real_provider_proof", report.get("real_provider_proof"), "Stage 27B report must have real_provider_proof=false"))
    if not isinstance(report.get("aggregate_results"), dict):
        failures.append(_failure("aggregate_results", type(report.get("aggregate_results")).__name__, "aggregate_results must be an object"))
    if not isinstance(report.get("per_lesson_results"), list) or not report.get("per_lesson_results"):
        failures.append(_failure("per_lesson_results", type(report.get("per_lesson_results")).__name__, "per_lesson_results must be a non-empty list"))
    if not effective_final_result(report):
        failures.append(_failure("final_result", None, "report must include overall_result or final_result"))
    return failures


def evaluate_thresholds(
    *,
    args: argparse.Namespace,
    report: dict[str, Any],
    final_result: str,
    connected_clients_total: int,
    receive_rate: float,
    latency: dict[str, float | None],
    disconnects: int,
    error_count: int,
    ws_timeout_delta: int,
    ws_failure_delta: int,
    ws_drop_delta: int,
    per_lesson: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if final_result == "fail":
        failures.append(_failure("final_result", final_result, "Stage 27B report final result is fail"))
    if args.require_final_result_pass and final_result != "pass":
        failures.append(_failure("final_result", final_result, "Stage 27B report final result must be pass"))
    if report.get("mock_only") is not True:
        failures.append(_failure("mock_only", report.get("mock_only"), "mock_only must be true"))
    if report.get("real_provider_proof") is not False:
        failures.append(_failure("real_provider_proof", report.get("real_provider_proof"), "real_provider_proof must be false"))
    if connected_clients_total < _int(report.get("students")):
        failures.append(_failure("connected_clients_total", connected_clients_total, "connected clients must meet requested students"))
    if receive_rate < float(args.min_receive_rate):
        failures.append(_failure("receive_rate", receive_rate, f"receive_rate must be >= {args.min_receive_rate}"))
    if latency["p95_ms"] is None or latency["p95_ms"] > float(args.max_p95_latency_ms):
        failures.append(_failure("p95_latency_ms", latency["p95_ms"], f"p95 latency must be <= {args.max_p95_latency_ms} ms"))
    if latency["p99_ms"] is None or latency["p99_ms"] > float(args.max_p99_latency_ms):
        failures.append(_failure("p99_latency_ms", latency["p99_ms"], f"p99 latency must be <= {args.max_p99_latency_ms} ms"))
    disconnect_rate = disconnects / max(1, _int(report.get("students")))
    if disconnect_rate > float(args.max_disconnect_rate):
        failures.append(_failure("disconnect_rate", round(disconnect_rate, 6), f"disconnect rate must be <= {args.max_disconnect_rate}"))
    if error_count > int(args.max_error_count):
        failures.append(_failure("errors_count", error_count, f"errors count must be <= {args.max_error_count}"))
    if ws_timeout_delta > int(args.max_ws_timeout_count):
        failures.append(_failure("websocket_send_timeouts_total_delta", ws_timeout_delta, f"WebSocket timeout delta must be <= {args.max_ws_timeout_count}"))
    if final_result == "pass" and ws_failure_delta > 0:
        failures.append(_failure("websocket_send_failures_total_delta", ws_failure_delta, "WebSocket send failures should not increase in a passing report"))
    if final_result == "pass" and ws_drop_delta > 0:
        failures.append(_failure("websocket_clients_dropped_total_delta", ws_drop_delta, "WebSocket dropped-client counter should not increase in a passing report"))
    severe_lesson_imbalance = severe_receive_imbalance(per_lesson, args.min_receive_rate)
    if severe_lesson_imbalance:
        failures.append(_failure("per_lesson_receive_imbalance", severe_lesson_imbalance, "one or more lessons had severe receive-rate imbalance"))
    return failures


def build_fanout_health(
    *,
    report: dict[str, Any],
    runtime_before: dict[str, Any],
    runtime_after: dict[str, Any],
    connected_clients_total: int,
    students: int,
    receive_rate: float,
    args: argparse.Namespace,
) -> dict[str, Any]:
    diagnostics = _dict(report.get("diagnostics"))
    warnings: list[str] = []
    server_broadcast_p95 = _number_or_none(runtime_after.get("caption_broadcast_latency_ms_p95"))
    redis_pubsub_p95 = _number_or_none(runtime_after.get("redis_pubsub_latency_ms_p95"))
    runtime_percentiles_seen = any(
        key in runtime_before or key in runtime_after
        for key in ("caption_broadcast_latency_ms_p95", "redis_pubsub_latency_ms_p95")
    )
    redis_published_delta = metric_delta(runtime_before, runtime_after, "redis_pubsub_messages_published_total")
    redis_received_delta = metric_delta(runtime_before, runtime_after, "redis_pubsub_messages_received_total")
    redis_errors_delta = metric_delta(runtime_before, runtime_after, "redis_pubsub_errors_total")
    redis_counters_seen = any(
        key in runtime_before or key in runtime_after
        for key in ("redis_pubsub_messages_published_total", "redis_pubsub_messages_received_total", "redis_pubsub_errors_total")
    )
    ws_timeouts_before_shutdown = _diagnostic_delta_or_metric_delta(
        diagnostics,
        runtime_before,
        runtime_after,
        "websocket_send_timeouts_total",
    )
    ws_failures_before_shutdown = _diagnostic_delta_or_metric_delta(
        diagnostics,
        runtime_before,
        runtime_after,
        "websocket_send_failures_total",
    )
    ws_drops_before_shutdown = _diagnostic_delta_or_metric_delta(
        diagnostics,
        runtime_before,
        runtime_after,
        "websocket_clients_dropped_total",
    )
    ws_drops_after_shutdown = diagnostics.get("websocket_clients_dropped_total_delta_after_shutdown")
    if ws_drops_before_shutdown == 0 and _float(ws_drops_after_shutdown) > 0:
        warnings.append("Drops appeared after shutdown only; likely cleanup/cancellation artifact, not in-run capacity loss.")

    if runtime_percentiles_seen and server_broadcast_p95 is None:
        warnings.append("server caption broadcast p95 is missing")
    if runtime_percentiles_seen and redis_counters_seen and redis_pubsub_p95 is None:
        warnings.append("Redis Pub/Sub p95 is missing")
    elif redis_pubsub_p95 is not None and redis_pubsub_p95 > float(args.max_redis_pubsub_p95_ms) * 0.8:
        warnings.append("Redis Pub/Sub p95 is elevated but under the configured hard threshold.")

    provider_error_total = sum(
        metric_delta(runtime_before, runtime_after, key)
        for key in (
            "provider_timeout_errors_total",
            "provider_rate_limit_errors_total",
            "provider_auth_errors_total",
            "provider_unknown_errors_total",
        )
    )
    return {
        "connected_clients_ok": connected_clients_total >= students,
        "receive_rate_ok": receive_rate >= float(args.min_receive_rate),
        "server_broadcast_p95_ms": server_broadcast_p95,
        "server_broadcast_p95_threshold_ms": float(args.max_server_broadcast_p95_ms),
        "server_broadcast_p95_ok": server_broadcast_p95 is None or server_broadcast_p95 <= float(args.max_server_broadcast_p95_ms),
        "redis_pubsub_p95_ms": redis_pubsub_p95,
        "redis_pubsub_p95_threshold_ms": float(args.max_redis_pubsub_p95_ms),
        "redis_pubsub_p95_ok": redis_pubsub_p95 is None or redis_pubsub_p95 <= float(args.max_redis_pubsub_p95_ms),
        "redis_publish_receive_match": (not redis_counters_seen) or redis_published_delta == redis_received_delta,
        "redis_errors_ok": (not redis_counters_seen) or redis_errors_delta == 0,
        "ws_timeouts_ok": ws_timeouts_before_shutdown <= int(args.max_ws_timeout_count),
        "ws_failures_ok": ws_failures_before_shutdown == 0,
        "ws_drops_ok": ws_drops_before_shutdown == 0,
        "provider_errors_ok": provider_error_total == 0,
        "provider_error_count": provider_error_total,
        "warnings": warnings,
    }


def build_client_receive_health(
    *,
    latency: dict[str, float | None],
    receive_rate: float,
    disconnects: int,
    error_count: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    return {
        "client_receive_p95_ms": latency["p95_ms"],
        "client_receive_p95_threshold_ms": float(args.max_p95_latency_ms),
        "client_receive_p95_ok": latency["p95_ms"] is not None and latency["p95_ms"] <= float(args.max_p95_latency_ms),
        "client_receive_p99_ms": latency["p99_ms"],
        "client_receive_p99_threshold_ms": float(args.max_p99_latency_ms),
        "client_receive_p99_ok": latency["p99_ms"] is not None and latency["p99_ms"] <= float(args.max_p99_latency_ms),
        "receive_rate": receive_rate,
        "receive_rate_ok": receive_rate >= float(args.min_receive_rate),
        "disconnects": disconnects,
        "disconnects_ok": disconnects == 0,
        "client_errors": error_count,
        "client_errors_ok": error_count <= int(args.max_error_count),
    }


def choose_backend_fanout_verdict(health: dict[str, Any]) -> str:
    hard_fail_keys = (
        "connected_clients_ok",
        "receive_rate_ok",
        "server_broadcast_p95_ok",
        "redis_pubsub_p95_ok",
        "redis_publish_receive_match",
        "redis_errors_ok",
        "ws_timeouts_ok",
        "ws_failures_ok",
        "ws_drops_ok",
        "provider_errors_ok",
    )
    if any(health.get(key) is False for key in hard_fail_keys):
        return FAIL
    if health.get("warnings"):
        return PASS_WITH_WARNINGS
    return PASS


def choose_client_receive_verdict(health: dict[str, Any]) -> str:
    if (
        not health.get("client_receive_p95_ok")
        or not health.get("client_receive_p99_ok")
        or not health.get("receive_rate_ok")
        or int(health.get("disconnects") or 0) > 0
        or int(health.get("client_errors") or 0) > 0
    ):
        return FAIL
    return PASS


def choose_overall_verdict(
    *,
    backend_fanout_verdict: str,
    client_receive_verdict: str,
    client_receive_health: dict[str, Any],
    args: argparse.Namespace,
    warnings: list[str],
) -> str:
    if backend_fanout_verdict == FAIL:
        return FAIL
    if client_receive_verdict == FAIL:
        if bool(getattr(args, "allow_backend_pass_client_fail", False)) and _client_failed_only_latency(client_receive_health):
            return INCONCLUSIVE
        return FAIL
    if backend_fanout_verdict == PASS_WITH_WARNINGS or warnings:
        return PASS_WITH_WARNINGS
    return PASS


def _client_failed_only_latency(health: dict[str, Any]) -> bool:
    return (
        health.get("receive_rate_ok") is True
        and int(health.get("disconnects") or 0) == 0
        and int(health.get("client_errors") or 0) == 0
        and health.get("client_receive_p99_ok") is True
        and health.get("client_receive_p95_ok") is False
    )


def build_warnings(
    *,
    report: dict[str, Any],
    per_lesson: list[dict[str, Any]],
    runtime_before: dict[str, Any],
    runtime_after: dict[str, Any],
    runtime_during: list[Any],
    disconnects: int,
    students: int,
    latency: dict[str, float | None],
    args: argparse.Namespace,
) -> list[str]:
    warnings: list[str] = []
    if not runtime_before or not runtime_after or not runtime_during:
        warnings.append("runtime metrics snapshots are missing or incomplete")
    if not redis_counters_present(runtime_before, runtime_after):
        warnings.append("Redis Pub/Sub counters are missing; multi-worker fanout evidence is weaker")
    if uneven_distribution(per_lesson):
        warnings.append("student distribution across lessons is uneven")
    if 0 < disconnects <= int(float(args.max_disconnect_rate) * max(1, students)):
        warnings.append("minor disconnects were observed within the configured threshold")
    if latency["p99_ms"] is not None and latency["p99_ms"] > float(args.max_p99_latency_ms) * 0.8:
        warnings.append("p99 latency is high even though required latency thresholds passed")
    if effective_final_result(report) == "partial":
        warnings.append("Stage 27B report result is partial")
    return warnings


def bottleneck_hints(
    *,
    report: dict[str, Any],
    per_lesson: list[dict[str, Any]],
    runtime_before: dict[str, Any],
    runtime_after: dict[str, Any],
    fanout_health: dict[str, Any],
    backend_fanout_verdict: str,
    client_receive_verdict: str,
    connected_clients_total: int,
    students: int,
    receive_rate: float,
    latency: dict[str, float | None],
    disconnects: int,
    error_count: int,
    ws_timeout_delta: int,
    ws_failure_delta: int,
    ws_drop_delta: int,
    args: argparse.Namespace,
) -> list[str]:
    hints: list[str] = []
    redis_published_delta = metric_delta(runtime_before, runtime_after, "redis_pubsub_messages_published_total")
    redis_received_delta = metric_delta(runtime_before, runtime_after, "redis_pubsub_messages_received_total")
    redis_errors_delta = metric_delta(runtime_before, runtime_after, "redis_pubsub_errors_total")
    redis_healthy = redis_published_delta > 0 and redis_published_delta == redis_received_delta and redis_errors_delta == 0
    high_p95 = (latency["p95_ms"] or 0) > float(args.max_p95_latency_ms)
    if backend_fanout_verdict in {PASS, PASS_WITH_WARNINGS} and client_receive_verdict == FAIL:
        hints.append(
            "Backend fanout metrics are healthy while client receive p95 is high; suspect client generator, Docker Desktop, OS scheduling, network/proxy path, or timestamp scope."
        )
    if fanout_health.get("server_broadcast_p95_ok") is False:
        hints.append("Server broadcast p95 is high; inspect CaptionHub concurrency, event loop pressure, and per-lesson fanout.")
    if fanout_health.get("redis_pubsub_p95_ok") is False:
        hints.append("Redis Pub/Sub p95 is high; inspect Redis latency, container networking, Pub/Sub listener scheduling.")
    for warning in fanout_health.get("warnings") or []:
        if "Drops appeared after shutdown only" in warning:
            hints.append(warning)
    if (latency["p95_ms"] or 0) > float(args.max_p95_latency_ms) and ws_timeout_delta > 0:
        hints.append("High p95 latency plus WebSocket timeouts points to slow clients or proxy/network pressure.")
    elif (latency["p95_ms"] or 0) > float(args.max_p95_latency_ms):
        hints.append("High p95 latency points to server broadcast pressure, slow clients, or proxy/network buffering.")
    if high_p95 and _slightly_above(latency["p95_ms"], float(args.max_p95_latency_ms)):
        hints.append("p95 is only slightly above the configured threshold; rerun with lower captions/sec and on a stronger host before treating this as a hard app ceiling.")
    if ws_failure_delta > 0 and ws_timeout_delta == 0:
        hints.append("WebSocket send failures increased while send timeouts stayed at 0; this usually means sends hit already-closing sockets rather than slow-client timeout protection.")
    if ws_drop_delta > 0 and connected_clients_total >= students:
        hints.append("All requested clients connected, but dropped-client counters increased; compare in-run metrics with after-shutdown metrics to separate real drops from shutdown cleanup artifacts.")
    if _counter_appeared_after_last_snapshot(runtime_during=report.get("runtime_metrics_during"), runtime_after=runtime_after, key="websocket_send_failures_total"):
        hints.append("WebSocket send failures appear only after the last in-run snapshot, so shutdown ordering or client cancellation race is a likely contributor.")
    if _counter_appeared_after_last_snapshot(runtime_during=report.get("runtime_metrics_during"), runtime_after=runtime_after, key="websocket_clients_dropped_total"):
        hints.append("Dropped-client counters appear only after the last in-run snapshot, which is consistent with cleanup/shutdown artifacts unless a pre-shutdown snapshot shows otherwise.")
    if redis_healthy and high_p95:
        hints.append("Redis Pub/Sub counters look healthy while p95 is high; focus next on the WebSocket broadcast path, event loop pressure, and client-side receive scheduling.")
    server_broadcast_p95 = _float(runtime_after.get("caption_broadcast_latency_ms_p95"))
    if high_p95 and 0 < server_broadcast_p95 < float(args.max_p95_latency_ms) * 0.25:
        hints.append(
            "client receive p95 is high while server broadcast p95 is low; focus on proxy/network buffering, client generator scheduling, or Redis/listener delay."
        )
    if redis_healthy and _float(runtime_after.get("redis_pubsub_latency_ms_avg")) >= 100:
        hints.append("Redis Pub/Sub publish/receive counts match with zero errors, but average Pub/Sub latency is around or above 100 ms; treat Redis as healthy but still worth profiling under Docker/local load.")
    if redis_healthy and _float(runtime_after.get("redis_pubsub_latency_ms_p95")) >= 100:
        hints.append("Redis Pub/Sub p95 is elevated even with matched publish/receive counts; profile Redis, listener scheduling, and container/network placement.")
    if connected_clients_total < students:
        hints.append("Connected clients below requested students suggests connection capacity, proxy, file descriptor, or port exhaustion limits.")
    weak_lessons = [lesson for lesson in per_lesson if _float(lesson.get("receive_rate")) < float(args.min_receive_rate)]
    if len(weak_lessons) == 1:
        hints.append(f"Low receive rate on one lesson only ({weak_lessons[0].get('lesson_id')}) suggests a per-lesson hub, publisher, or routing issue.")
    elif len(weak_lessons) > 1 and receive_rate < float(args.min_receive_rate):
        hints.append("Low receive rate across multiple lessons suggests broadcast capacity or shared infrastructure pressure.")
    if redis_publish_receive_mismatch(runtime_before, runtime_after):
        hints.append("Redis publish/receive mismatch suggests a Pub/Sub listener, worker routing, or Redis connectivity issue.")
    if disconnects > 0:
        hints.append("High disconnects can indicate proxy timeout, client pressure, server resource pressure, or network churn.")
    if error_count > 0:
        hints.append("Client or report errors were recorded; inspect sanitized errors before using this as readiness evidence.")
    if ws_timeout_delta > 0 and not any("slow clients or proxy/network pressure" in hint for hint in hints):
        hints.append("WebSocket send timeouts increased; investigate slow clients or proxy/network pressure.")
    if connected_clients_total >= 1000 and high_p95 and redis_healthy:
        hints.append("A single Python process driving 1000 clients can become the bottleneck; rerun from a stronger/Linux client host or split clients across generators to confirm.")
    if not hints:
        hints.append("No obvious bottleneck detected in the Stage 27B mock WebSocket fanout report.")
    return hints


def choose_verdict(failed_thresholds: list[dict[str, Any]], warnings: list[str]) -> str:
    if failed_thresholds:
        return FAIL
    if warnings:
        return PASS_WITH_WARNINGS
    return PASS


def per_lesson_summary(per_lesson: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "lesson_id": lesson.get("lesson_id"),
            "students_connected": _int(lesson.get("students_connected")),
            "captions_published": _int(lesson.get("captions_published")),
            "total_received": _int(lesson.get("total_received")),
            "receive_rate": _float(lesson.get("receive_rate")),
            "p50_latency_ms": _number_or_none(lesson.get("p50_caption_latency_ms")),
            "p95_latency_ms": _number_or_none(lesson.get("p95_caption_latency_ms")),
            "p99_latency_ms": _number_or_none(lesson.get("p99_caption_latency_ms")),
            "disconnects": _int(lesson.get("disconnects")),
            "errors": _int(lesson.get("errors")),
        }
        for lesson in per_lesson
        if isinstance(lesson, dict)
    ]


def severe_receive_imbalance(per_lesson: list[dict[str, Any]], min_receive_rate: float) -> list[dict[str, Any]]:
    if not per_lesson:
        return []
    rates = [_float(lesson.get("receive_rate")) for lesson in per_lesson if isinstance(lesson, dict)]
    if not rates:
        return []
    severe_cutoff = min(float(min_receive_rate), max(0.0, max(rates) - 0.10))
    return [
        {"lesson_id": lesson.get("lesson_id"), "receive_rate": _float(lesson.get("receive_rate"))}
        for lesson in per_lesson
        if isinstance(lesson, dict) and _float(lesson.get("receive_rate")) < severe_cutoff
    ]


def uneven_distribution(per_lesson: list[dict[str, Any]]) -> bool:
    counts = [_int(lesson.get("students_connected")) for lesson in per_lesson if isinstance(lesson, dict)]
    return bool(counts) and max(counts) - min(counts) > 1


def redis_counters_present(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return any(
        key in before or key in after
        for key in ("redis_pubsub_messages_published_total", "redis_pubsub_messages_received_total", "redis_pubsub_errors_total")
    )


def redis_publish_receive_mismatch(before: dict[str, Any], after: dict[str, Any]) -> bool:
    published = metric_delta(before, after, "redis_pubsub_messages_published_total")
    received = metric_delta(before, after, "redis_pubsub_messages_received_total")
    if published <= 0 and received <= 0:
        return False
    return abs(published - received) > max(1, int(published * 0.05))


def _slightly_above(value: float | None, threshold: float) -> bool:
    if value is None or threshold <= 0:
        return False
    return threshold < float(value) <= threshold * 1.10


def _counter_appeared_after_last_snapshot(*, runtime_during: Any, runtime_after: dict[str, Any], key: str) -> bool:
    if not isinstance(runtime_during, list) or not runtime_during:
        return False
    last_snapshot = next((item for item in reversed(runtime_during) if isinstance(item, dict) and key in item), None)
    if last_snapshot is None:
        return False
    return _int(last_snapshot.get(key)) == 0 and _int(runtime_after.get(key)) > 0


def metric_delta(before: dict[str, Any], after: dict[str, Any], key: str) -> int:
    return _int(after.get(key)) - _int(before.get(key))


def _diagnostic_delta_or_metric_delta(diagnostics: dict[str, Any], before: dict[str, Any], after: dict[str, Any], key: str) -> int:
    diagnostic_key = f"{key}_delta_before_shutdown"
    if diagnostic_key in diagnostics:
        return _int(diagnostics.get(diagnostic_key))
    return metric_delta(before, after, key)


def scenario(report: dict[str, Any]) -> str:
    lessons = _int(report.get("lessons"))
    students = _int(report.get("students"))
    return f"stage27b_{lessons}_lessons_{students}_caption_websocket_mock_load"


def effective_final_result(report: dict[str, Any]) -> str:
    return str(report.get("final_result") or report.get("overall_result") or "").lower()


def invalid_analysis(failed_thresholds: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "verdict": INVALID_REPORT,
        "overall_verdict": INVALID_REPORT,
        "backend_fanout_verdict": UNKNOWN,
        "client_receive_verdict": UNKNOWN,
        "fanout_health": {},
        "client_receive_health": {},
        "scenario": None,
        "mock_only": None,
        "real_provider_proof": None,
        "students": None,
        "lessons": None,
        "connected_clients_total": None,
        "receive_rate": None,
        "latency": {"p50_ms": None, "p95_ms": None, "p99_ms": None},
        "disconnects": None,
        "errors_count": None,
        "per_lesson_summary": [],
        "failed_thresholds": failed_thresholds,
        "warnings": ["report is missing required Stage 27B fields or is not a Stage 27B report"],
        "bottleneck_hints": ["Cannot infer bottlenecks from an invalid or malformed report."],
        "mock_websocket_fanout_only": True,
        "real_provider_capacity_proven": False,
    }


def read_report(path: Path) -> tuple[Any, list[dict[str, Any]]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except FileNotFoundError:
        return None, [_failure("report_json", str(path), "report JSON file does not exist")]
    except json.JSONDecodeError as exc:
        return None, [_failure("malformed_json", f"line {exc.lineno} column {exc.colno}", "report JSON is malformed")]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    for name, value in DEFAULT_ANALYZER_ARGS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    return args


def _failure(name: str, value: Any, message: str) -> dict[str, Any]:
    return {"name": name, "value": value, "message": message}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report, read_failures = read_report(Path(args.report_json))
    if read_failures:
        analysis = invalid_analysis(read_failures)
    else:
        analysis = analyze_report(report, args)
    write_json(Path(args.output_json), analysis)
    print(json.dumps(analysis, indent=2, ensure_ascii=False, sort_keys=True))
    verdict = analysis.get("verdict")
    if verdict in {PASS, PASS_WITH_WARNINGS}:
        return 0
    if verdict == INVALID_REPORT:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
