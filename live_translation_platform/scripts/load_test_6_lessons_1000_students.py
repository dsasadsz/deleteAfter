#!/usr/bin/env python
"""True mock-provider 6-lesson / 1000 caption WebSocket load test."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SECRET_KEY_RE = re.compile(
    r"(secret|token|api[_-]?key|apikey|password|passwd|authorization|credential|signature|integration[_-]?key|cookie|database[_-]?url|redis[_-]?url)",
    re.IGNORECASE,
)
SECRET_QUERY_NAMES = {
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "key",
    "password",
    "pwd",
    "signature",
    "integration_key",
    "integration-key",
    "sig",
}
SECRET_QUERY_RE = re.compile(
    r"([?&](?:token|access_token|refresh_token|api_key|apikey|key|password|pwd|signature|integration_key|integration-key|sig)=)[^&#\s]+",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
AUTH_HEADER_RE = re.compile(r"(?i)(Authorization\s*:\s*)(?:Bearer\s+)?[^\s,;]+")
USERINFO_URL_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/@\s]+:)[^/@\s]+(@)", re.IGNORECASE)
MAX_CLIENT_LATENCY_SAMPLES = 500

RUNTIME_METRIC_KEYS = (
    "caption_ws_clients",
    "captions_sent_total",
    "captions_per_second",
    "websocket_send_timeouts_total",
    "websocket_send_failures_total",
    "websocket_clients_dropped_total",
    "caption_broadcast_latency_ms_avg",
    "caption_broadcast_latency_ms_p50",
    "caption_broadcast_latency_ms_p95",
    "caption_broadcast_latency_ms_p99",
    "redis_pubsub_messages_published_total",
    "redis_pubsub_messages_received_total",
    "redis_pubsub_errors_total",
    "redis_pubsub_latency_ms_avg",
    "redis_pubsub_latency_ms_p50",
    "redis_pubsub_latency_ms_p95",
    "redis_pubsub_latency_ms_p99",
    "translation_latency_ms_p50",
    "translation_latency_ms_p95",
    "translation_latency_ms_p99",
    "tts_latency_ms_p50",
    "tts_latency_ms_p95",
    "tts_latency_ms_p99",
    "stt_latency_ms_p50",
    "stt_latency_ms_p95",
    "stt_latency_ms_p99",
    "provider_errors_by_provider",
    "provider_timeouts_by_provider",
    "provider_timeout_errors_total",
    "provider_rate_limit_errors_total",
    "provider_auth_errors_total",
    "provider_unknown_errors_total",
    "cpu_percent",
    "memory_rss_bytes",
)
DELTA_FAILURE_METRICS = (
    "websocket_send_timeouts_total",
    "websocket_send_failures_total",
    "websocket_clients_dropped_total",
    "redis_pubsub_errors_total",
)


@dataclass
class ClientStats:
    client_id: int
    lesson_id: str
    connected: bool = False
    received_count: int = 0
    first_caption_latency_ms: float | None = None
    last_caption_at: str | None = None
    disconnect_reason: str | None = None
    error: str | None = None
    latencies_ms: list[float] | None = None

    def add_latency(self, latency_ms: float) -> None:
        if self.latencies_ms is None:
            self.latencies_ms = []
        self.latencies_ms.append(latency_ms)
        if len(self.latencies_ms) > MAX_CLIENT_LATENCY_SAMPLES:
            del self.latencies_ms[: len(self.latencies_ms) - MAX_CLIENT_LATENCY_SAMPLES]
        if self.first_caption_latency_ms is None:
            self.first_caption_latency_ms = latency_ms


@dataclass(frozen=True)
class ThresholdEvaluation:
    overall_result: str
    checks: list[dict[str, Any]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Open real caption WebSocket clients across 6 mock lessons and publish mock captions. "
            "This is mock fanout proof, not real STT/translation/TTS/Zoom proof."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="HTTP application base URL.")
    parser.add_argument("--ws-base-url", default="ws://127.0.0.1:8000", help="WebSocket application base URL.")
    parser.add_argument("--lessons", type=int, default=6, help="Number of mock lessons.")
    parser.add_argument("--lesson-id", action="append", default=[], help="Existing mock lesson id to reuse. Repeatable.")
    parser.add_argument("--students", type=int, default=1000, help="Number of caption WebSocket clients.")
    parser.add_argument("--duration-seconds", type=float, default=120, help="Run duration in seconds.")
    parser.add_argument("--captions-per-second", type=float, default=3, help="Mock captions per second per lesson.")
    parser.add_argument("--student-distribution", default="even", choices=["even"], help="How students are assigned to lessons.")
    parser.add_argument("--report-json", default="tmp/6_lessons_1000_students_report.json", help="JSON report path.")
    parser.add_argument("--assert-min-receive-rate", type=float, default=0.99, help="Minimum aggregate receive rate.")
    parser.add_argument("--assert-p95-caption-latency-ms", type=float, default=1000, help="Maximum p95 caption latency.")
    parser.add_argument(
        "--allow-dev-load-test-endpoints",
        action="store_true",
        help="Permit use of the dev-only mock caption publishing endpoint.",
    )
    parser.add_argument("--integration-key", default="", help="Optional integration key. Never printed or written unredacted.")
    parser.add_argument("--student-token", default="", help="Optional student caption token if reusing an authenticated flow.")
    parser.add_argument("--slow-client-percent", type=float, default=0.0, help="Percent of clients that delay after receive.")
    parser.add_argument("--slow-client-delay-ms", type=float, default=0.0, help="Delay per slow-client receive in milliseconds.")
    parser.add_argument(
        "--no-fail-on-thresholds",
        action="store_true",
        help="Write a partial report and exit 0 when thresholds fail; useful for exploratory runs.",
    )
    return parser


def distribute_students(lesson_ids: list[str], students: int, distribution: str = "even") -> dict[str, int]:
    if distribution != "even":
        raise ValueError(f"Unsupported student distribution: {distribution}")
    if not lesson_ids:
        return {}
    total = max(0, int(students))
    base = total // len(lesson_ids)
    remainder = total % len(lesson_ids)
    return {lesson_id: base + (1 if index < remainder else 0) for index, lesson_id in enumerate(lesson_ids)}


def mock_caption_payload(sequence: int, lesson_id: str, published_at_utc: str) -> dict[str, Any]:
    return {
        "sequence": int(sequence),
        "original_text": f"Mock load-test caption {sequence} for {lesson_id}",
        "translations": {
            "kk": f"Mock load-test caption {sequence} kk",
            "uz": f"Mock load-test caption {sequence} uz",
        },
        "source_language": "ru-RU",
        "speaker_id": "stage27b-load-test",
        "speaker_name": "Stage 27B Load Test",
        "is_partial": False,
        "latency_ms": {"stt": 0, "translation": 0, "total": 0},
        "load_test_client_published_at": published_at_utc,
    }


def build_report(
    *,
    args: argparse.Namespace,
    lesson_ids: list[str],
    clients: list[ClientStats],
    captions_published_by_lesson: dict[str, int],
    runtime_metrics_before: dict[str, Any],
    runtime_metrics_during: list[dict[str, Any]],
    runtime_metrics_after: dict[str, Any],
    errors: list[Any],
    run_started_at_utc: str,
    run_finished_at_utc: str,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    per_lesson_results = []
    for lesson_id in lesson_ids:
        lesson_clients = [client for client in clients if client.lesson_id == lesson_id]
        per_lesson_results.append(_lesson_result(lesson_id, lesson_clients, captions_published_by_lesson.get(lesson_id, 0)))
    aggregate = _aggregate_result(clients, captions_published_by_lesson)
    report = {
        "run_started_at_utc": run_started_at_utc,
        "run_finished_at_utc": run_finished_at_utc,
        "base_url": args.base_url,
        "ws_base_url": args.ws_base_url,
        "lessons": int(args.lessons),
        "students": int(args.students),
        "duration_seconds": float(args.duration_seconds),
        "captions_per_second": float(args.captions_per_second),
        "student_distribution": args.student_distribution,
        "thresholds": {
            "min_receive_rate": float(args.assert_min_receive_rate),
            "p95_caption_latency_ms": float(args.assert_p95_caption_latency_ms),
            "websocket_send_timeouts_total_delta_max": 0,
            "websocket_send_failures_total_delta_max": 0,
            "websocket_clients_dropped_total_delta_max": 0,
            "redis_pubsub_errors_total_delta_max": 0,
        },
        "safety_notes": [
            "Mock load proof only; no real STT, translation, TTS, or Zoom provider calls are made.",
            "Requires local/staging load-test mode for mock caption publishing.",
            "Production must keep load-test endpoints disabled by default.",
            "mock WebSocket load is not real-provider proof.",
        ],
        "overall_result": "partial",
        "per_lesson_results": per_lesson_results,
        "aggregate_results": aggregate,
        "runtime_metrics_before": filter_runtime_metrics(runtime_metrics_before),
        "runtime_metrics_during": [filter_runtime_metrics(snapshot) for snapshot in runtime_metrics_during],
        "runtime_metrics_after": filter_runtime_metrics(runtime_metrics_after),
        "diagnostics": build_diagnostics(
            runtime_metrics_before=runtime_metrics_before,
            runtime_metrics_during=runtime_metrics_during,
            runtime_metrics_after=runtime_metrics_after,
            diagnostics=diagnostics,
        ),
        "errors_sanitized": sanitize_for_report(errors),
        "mock_only": True,
        "real_provider_proof": False,
        "threshold_checks": [],
    }
    evaluation = evaluate_thresholds(report, no_fail_on_thresholds=bool(getattr(args, "no_fail_on_thresholds", False)))
    report["overall_result"] = evaluation.overall_result
    report["threshold_checks"] = evaluation.checks
    return sanitize_for_report(report)


def evaluate_thresholds(report: dict[str, Any], *, no_fail_on_thresholds: bool) -> ThresholdEvaluation:
    aggregate = report.get("aggregate_results") if isinstance(report.get("aggregate_results"), dict) else {}
    thresholds = report.get("thresholds") if isinstance(report.get("thresholds"), dict) else {}
    before = report.get("runtime_metrics_before") if isinstance(report.get("runtime_metrics_before"), dict) else {}
    after = report.get("runtime_metrics_after") if isinstance(report.get("runtime_metrics_after"), dict) else {}
    requested_students = int(report.get("students", 0) or 0)
    checks = [
        _check(
            "connected_students",
            aggregate.get("students_connected"),
            int(aggregate.get("students_connected", 0) or 0) >= requested_students,
            f"connected students must equal requested students ({requested_students})",
        ),
        _check(
            "receive_rate",
            aggregate.get("receive_rate"),
            float(aggregate.get("receive_rate", 0.0) or 0.0) >= float(thresholds.get("min_receive_rate", 0.99) or 0.99),
            "aggregate receive rate meets threshold",
        ),
        _check(
            "p95_caption_latency_ms",
            aggregate.get("p95_caption_latency_ms"),
            (aggregate.get("p95_caption_latency_ms") is not None)
            and float(aggregate.get("p95_caption_latency_ms") or 0.0) <= float(thresholds.get("p95_caption_latency_ms", 1000) or 1000),
            "aggregate p95 caption latency meets threshold",
        ),
        _check(
            "unexpected_errors",
            len(report.get("errors_sanitized") or []),
            len(report.get("errors_sanitized") or []) == 0 and int(aggregate.get("errors", 0) or 0) == 0,
            "no unexpected HTTP/WebSocket/client errors",
        ),
    ]
    for metric in DELTA_FAILURE_METRICS:
        delta = _metric_delta(before, after, metric)
        checks.append(
            _check(
                f"{metric}_delta",
                delta,
                delta <= 0,
                f"{metric} must not increase during the run",
            )
        )
    failed = [check for check in checks if check["status"] == "fail"]
    if not failed:
        return ThresholdEvaluation("pass", checks)
    return ThresholdEvaluation("partial" if no_fail_on_thresholds else "fail", checks)


def build_diagnostics(
    *,
    runtime_metrics_before: dict[str, Any],
    runtime_metrics_during: list[dict[str, Any]],
    runtime_metrics_after: dict[str, Any],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    supplied = diagnostics if isinstance(diagnostics, dict) else {}
    before_shutdown = supplied.get("runtime_metrics_before_shutdown")
    before_shutdown_metrics = filter_runtime_metrics(before_shutdown) if isinstance(before_shutdown, dict) else {}
    snapshots = [runtime_metrics_before, *runtime_metrics_during, before_shutdown_metrics, runtime_metrics_after]
    payload: dict[str, Any] = {
        "peak_connected_clients": max((_metric_value(snapshot, "caption_ws_clients") for snapshot in snapshots), default=0),
        "runtime_metrics_peak": {
            "caption_ws_clients": max((_metric_value(snapshot, "caption_ws_clients") for snapshot in snapshots), default=0),
            "captions_per_second": max((_metric_value(snapshot, "captions_per_second") for snapshot in snapshots), default=0.0),
        },
    }
    for metric in DELTA_FAILURE_METRICS:
        if before_shutdown_metrics:
            payload[f"{metric}_delta_before_shutdown"] = _metric_delta(runtime_metrics_before, before_shutdown_metrics, metric)
        payload[f"{metric}_delta_after_shutdown"] = _metric_delta(runtime_metrics_before, runtime_metrics_after, metric)
    optional_keys = (
        "connection_ramp_up_seconds",
        "publish_started_at_utc",
        "publish_finished_at_utc",
        "publisher_request_latency_ms",
        "client_receive_loop_lag_ms",
        "host_app_resource_notes",
    )
    for key in optional_keys:
        if key in supplied:
            payload[key] = supplied[key]
    if before_shutdown_metrics:
        payload["runtime_metrics_before_shutdown"] = before_shutdown_metrics
    return sanitize_for_report(payload)


def filter_runtime_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    payload = metrics if isinstance(metrics, dict) else {}
    return {key: payload.get(key) for key in RUNTIME_METRIC_KEYS if key in payload}


def sanitize_for_report(value: Any, key: str | None = None) -> Any:
    if key and SECRET_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(child_key): sanitize_for_report(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [sanitize_for_report(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_report(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _sanitize_string(value)
    return value


async def run_async(args: argparse.Namespace) -> dict[str, Any]:
    _validate_args(args)
    errors: list[Any] = []
    runtime_metrics_during: list[dict[str, Any]] = []
    captions_published_by_lesson: dict[str, int] = {}
    publisher_request_latencies_ms: list[float] = []
    clients: list[ClientStats] = []
    started_at = utc_now()
    stop_event = asyncio.Event()

    print(
        "WARNING: Stage 27B is mock WebSocket fanout proof only; it does not prove real-provider capacity.",
        file=sys.stderr,
    )
    safe_mock_path = await asyncio.to_thread(confirm_safe_mock_path, args.base_url)
    if not args.allow_dev_load_test_endpoints and not safe_mock_path:
        raise SystemExit(
            "Refusing to publish mock captions: pass --allow-dev-load-test-endpoints or run against a confirmed safe local/staging mock path."
        )

    runtime_metrics_before = await asyncio.to_thread(fetch_json, args.base_url, "/api/metrics/runtime")
    lesson_ids = await asyncio.to_thread(create_or_reuse_lessons, args)
    distribution = distribute_students(lesson_ids, args.students, args.student_distribution)
    clients = _client_stats_for_distribution(distribution)

    receiver_tasks = [
        asyncio.create_task(
            receive_caption_client(
                client=client,
                ws_base_url=args.ws_base_url,
                student_token=args.student_token,
                stop_event=stop_event,
                slow_delay_ms=_slow_delay_for_client(client.client_id, args),
            )
        )
        for client in clients
    ]
    ramp_started_at = time.monotonic()
    await asyncio.sleep(min(5.0, max(0.5, float(args.duration_seconds) * 0.05)))
    connection_ramp_up_seconds = round(time.monotonic() - ramp_started_at, 3)
    runtime_metrics_after_ramp = await asyncio.to_thread(fetch_json, args.base_url, "/api/metrics/runtime")

    publish_started_at_utc = utc_now()
    publisher_tasks = [
        asyncio.create_task(
            publish_lesson_captions(
                args,
                lesson_id,
                stop_event,
                captions_published_by_lesson,
                errors,
                publisher_request_latencies_ms,
            )
        )
        for lesson_id in lesson_ids
    ]
    metrics_task = asyncio.create_task(collect_runtime_metrics(args.base_url, stop_event, runtime_metrics_during, errors))
    runtime_metrics_during.append(runtime_metrics_after_ramp)
    runtime_metrics_before_shutdown: dict[str, Any] = {}

    try:
        await asyncio.sleep(max(0.0, float(args.duration_seconds)))
    finally:
        try:
            runtime_metrics_before_shutdown = await asyncio.to_thread(fetch_json, args.base_url, "/api/metrics/runtime")
        except Exception as exc:
            errors.append({"runtime_metrics_before_shutdown_error": _safe_error(exc)})
        stop_event.set()
        await asyncio.gather(*publisher_tasks, return_exceptions=True)
        await asyncio.gather(metrics_task, return_exceptions=True)
        await asyncio.gather(*receiver_tasks, return_exceptions=True)
        publish_finished_at_utc = utc_now()

    runtime_metrics_after = await asyncio.to_thread(fetch_json, args.base_url, "/api/metrics/runtime")
    diagnostics = {
        "connection_ramp_up_seconds": connection_ramp_up_seconds,
        "publish_started_at_utc": publish_started_at_utc,
        "publish_finished_at_utc": publish_finished_at_utc,
        "runtime_metrics_before_shutdown": runtime_metrics_before_shutdown,
        "publisher_request_latency_ms": {
            "p50": percentile(publisher_request_latencies_ms, 50),
            "p95": percentile(publisher_request_latencies_ms, 95),
            "p99": percentile(publisher_request_latencies_ms, 99),
            "samples": len(publisher_request_latencies_ms),
        },
        "host_app_resource_notes": [
            "cpu_percent and memory_rss_bytes are present only when psutil is installed in the app container.",
            "Compare before-shutdown deltas with after-shutdown deltas to identify shutdown cleanup artifacts.",
        ],
    }
    return build_report(
        args=args,
        lesson_ids=lesson_ids,
        clients=clients,
        captions_published_by_lesson=captions_published_by_lesson,
        runtime_metrics_before=runtime_metrics_before,
        runtime_metrics_during=runtime_metrics_during,
        runtime_metrics_after=runtime_metrics_after,
        errors=errors,
        run_started_at_utc=started_at,
        run_finished_at_utc=utc_now(),
        diagnostics=diagnostics,
    )


async def receive_caption_client(
    *,
    client: ClientStats,
    ws_base_url: str,
    student_token: str,
    stop_event: asyncio.Event,
    slow_delay_ms: float,
) -> None:
    try:
        import websockets
    except Exception as exc:
        client.error = f"websockets_unavailable:{exc.__class__.__name__}"
        return

    uri = caption_ws_url(ws_base_url, client.lesson_id, student_token)
    try:
        async with websockets.connect(uri, open_timeout=15, ping_interval=20, close_timeout=5, max_queue=None) as websocket:
            client.connected = True
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                payload = _parse_message(raw)
                client.received_count += 1
                client.last_caption_at = utc_now()
                latency = caption_latency_ms(payload)
                if latency is not None:
                    client.add_latency(latency)
                if slow_delay_ms > 0:
                    await asyncio.sleep(slow_delay_ms / 1000.0)
    except Exception as exc:
        if not stop_event.is_set():
            client.error = exc.__class__.__name__
        else:
            client.disconnect_reason = exc.__class__.__name__


async def publish_lesson_captions(
    args: argparse.Namespace,
    lesson_id: str,
    stop_event: asyncio.Event,
    captions_published_by_lesson: dict[str, int],
    errors: list[Any],
    publisher_request_latencies_ms: list[float] | None = None,
) -> None:
    captions_per_second = max(0.0, float(args.captions_per_second))
    if captions_per_second <= 0:
        captions_published_by_lesson[lesson_id] = 0
        return
    interval = 1.0 / captions_per_second
    sequence = 0
    next_at = time.monotonic()
    while not stop_event.is_set():
        sequence += 1
        payload = mock_caption_payload(sequence, lesson_id, utc_now())
        try:
            request_started_at = time.perf_counter()
            await asyncio.to_thread(post_json, args.base_url, f"/api/load-test/lessons/{lesson_id}/publish-caption", payload)
            if publisher_request_latencies_ms is not None:
                publisher_request_latencies_ms.append(round((time.perf_counter() - request_started_at) * 1000, 2))
            captions_published_by_lesson[lesson_id] = sequence
        except Exception as exc:
            errors.append({"lesson_id": lesson_id, "publish_error": _safe_error(exc)})
            stop_event.set()
            return
        next_at += interval
        await asyncio.sleep(max(0.0, next_at - time.monotonic()))


async def collect_runtime_metrics(base_url: str, stop_event: asyncio.Event, snapshots: list[dict[str, Any]], errors: list[Any]) -> None:
    while not stop_event.is_set():
        try:
            snapshots.append(await asyncio.to_thread(fetch_json, base_url, "/api/metrics/runtime"))
        except Exception as exc:
            errors.append({"runtime_metrics_error": _safe_error(exc)})
        await asyncio.sleep(5.0)


def create_or_reuse_lessons(args: argparse.Namespace) -> list[str]:
    lesson_ids = [lesson_id for lesson_id in getattr(args, "lesson_id", []) if lesson_id]
    for index in range(max(0, int(args.lessons) - len(lesson_ids))):
        lesson = post_json(
            args.base_url,
            "/api/lessons",
            {
                "title": f"Stage 27B Mock Load Lesson {index + 1}",
                "mode": "mock",
                "stt_provider": "mock",
                "translation_provider": "mock",
            },
        )
        lesson_id = lesson.get("lesson_id")
        if not lesson_id:
            raise RuntimeError("lesson creation response did not include lesson_id")
        lesson_ids.append(str(lesson_id))
    return lesson_ids[: int(args.lessons)]


def confirm_safe_mock_path(base_url: str) -> bool:
    try:
        health = fetch_json(base_url, "/api/health")
    except Exception:
        return False
    env = str(health.get("env") or health.get("app_env") or "").lower()
    host = urlsplit(base_url).hostname or ""
    return env in {"development", "local", "staging", "load-test", "loadtest"} and host not in {"", "example.com"}


def fetch_json(base_url: str, path: str, timeout: float = 15.0) -> dict[str, Any]:
    request = Request(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        headers={"Accept": "application/json", "User-Agent": "stage27b-6-lessons-1000-ws/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"URL error: {exc.reason.__class__.__name__}") from exc


def post_json(base_url: str, path: str, payload: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    request = Request(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "stage27b-6-lessons-1000-ws/1.0"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"URL error: {exc.reason.__class__.__name__}") from exc


def caption_ws_url(ws_base_url: str, lesson_id: str, student_token: str = "") -> str:
    base = ws_base_url.rstrip("/") + f"/ws/lessons/{lesson_id}/captions"
    if not student_token:
        return base
    parts = urlsplit(base)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("token", student_token))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def caption_latency_ms(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    timestamp = (
        payload.get("load_test_client_published_at")
        or payload.get("load_test_published_at")
        or (payload.get("timestamps") or {}).get("websocket_sent_at")
    )
    if not timestamp:
        return None
    try:
        published_at = _parse_utc_timestamp(str(timestamp))
    except ValueError:
        return None
    return max(0.0, round((datetime.now(timezone.utc) - published_at).total_seconds() * 1000, 2))


def write_report(report: dict[str, Any], report_json: str | Path) -> Path:
    path = Path(report_json)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sanitize_for_report(report), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = asyncio.run(run_async(args))
    except KeyboardInterrupt:
        print("Interrupted; cancellation requested.", file=sys.stderr)
        return 130
    except SystemExit as exc:
        print(sanitize_for_report(str(exc)), file=sys.stderr)
        return 2
    except Exception as exc:
        payload = {
            "overall_result": "fail",
            "mock_only": True,
            "real_provider_proof": False,
            "errors_sanitized": [sanitize_for_report(_safe_error(exc))],
        }
        write_report(payload, args.report_json)
        print(json.dumps(payload, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    path = write_report(report, args.report_json)
    print(json.dumps({"overall_result": report["overall_result"], "report_json": str(path)}, indent=2, sort_keys=True))
    if report["overall_result"] == "fail" and not args.no_fail_on_thresholds:
        return 1
    return 0


def _client_stats_for_distribution(distribution: dict[str, int]) -> list[ClientStats]:
    clients: list[ClientStats] = []
    client_id = 0
    for lesson_id, count in distribution.items():
        for _ in range(count):
            clients.append(ClientStats(client_id=client_id, lesson_id=lesson_id))
            client_id += 1
    return clients


def _lesson_result(lesson_id: str, clients: list[ClientStats], captions_published: int) -> dict[str, Any]:
    received_counts = [client.received_count for client in clients]
    latencies = _latencies(clients)
    total_received = sum(received_counts)
    expected = max(1, captions_published * len(clients))
    return {
        "lesson_id": lesson_id,
        "students_connected": sum(1 for client in clients if client.connected),
        "captions_published": int(captions_published),
        "total_received": total_received,
        "min_received_per_client": min(received_counts) if received_counts else 0,
        "max_received_per_client": max(received_counts) if received_counts else 0,
        "avg_received_per_client": round(total_received / len(clients), 4) if clients else 0.0,
        "receive_rate": round(total_received / expected, 6) if expected else 0.0,
        "p50_caption_latency_ms": percentile(latencies, 50),
        "p95_caption_latency_ms": percentile(latencies, 95),
        "p99_caption_latency_ms": percentile(latencies, 99),
        "disconnects": sum(1 for client in clients if client.disconnect_reason),
        "errors": sum(1 for client in clients if client.error),
    }


def _aggregate_result(clients: list[ClientStats], captions_published_by_lesson: dict[str, int]) -> dict[str, Any]:
    total_received = sum(client.received_count for client in clients)
    expected = sum(captions_published_by_lesson.get(client.lesson_id, 0) for client in clients)
    latencies = _latencies(clients)
    return {
        "students_connected": sum(1 for client in clients if client.connected),
        "students_requested": len(clients),
        "captions_published": sum(captions_published_by_lesson.values()),
        "total_received": total_received,
        "receive_rate": round(total_received / max(1, expected), 6),
        "p50_caption_latency_ms": percentile(latencies, 50),
        "p95_caption_latency_ms": percentile(latencies, 95),
        "p99_caption_latency_ms": percentile(latencies, 99),
        "disconnects": sum(1 for client in clients if client.disconnect_reason),
        "errors": sum(1 for client in clients if client.error),
        "client_samples": [sanitize_for_report(asdict(client)) for client in clients[:20]],
    }


def _latencies(clients: list[ClientStats]) -> list[float]:
    values: list[float] = []
    for client in clients:
        samples = client.latencies_ms or []
        if samples:
            values.extend(samples)
        elif client.first_caption_latency_ms is not None:
            values.append(float(client.first_caption_latency_ms))
    return values


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 2)
    rank = (len(ordered) - 1) * (percent / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(float(ordered[int(rank)]), 2)
    weight = rank - lower
    return round(float(ordered[lower] * (1 - weight) + ordered[upper] * weight), 2)


def _check(name: str, value: Any, passed: bool, message: str) -> dict[str, Any]:
    return {"name": name, "status": "pass" if passed else "fail", "value": value, "message": message}


def _metric_delta(before: dict[str, Any], after: dict[str, Any], metric: str) -> float:
    return float(after.get(metric, 0) or 0) - float(before.get(metric, 0) or 0)


def _metric_value(snapshot: dict[str, Any], metric: str) -> float:
    if not isinstance(snapshot, dict):
        return 0.0
    try:
        return float(snapshot.get(metric, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_message(raw: Any) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
    return raw


def _parse_utc_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _slow_delay_for_client(client_id: int, args: argparse.Namespace) -> float:
    percent = max(0.0, min(100.0, float(getattr(args, "slow_client_percent", 0.0) or 0.0)))
    if percent <= 0:
        return 0.0
    bucket = int(round(100 / percent)) if percent < 100 else 1
    return float(getattr(args, "slow_client_delay_ms", 0.0) or 0.0) if client_id % bucket == 0 else 0.0


def _validate_args(args: argparse.Namespace) -> None:
    if int(args.lessons) <= 0:
        raise SystemExit("--lessons must be greater than zero")
    if int(args.students) <= 0:
        raise SystemExit("--students must be greater than zero")
    if float(args.duration_seconds) <= 0:
        raise SystemExit("--duration-seconds must be greater than zero")
    if float(args.captions_per_second) <= 0:
        raise SystemExit("--captions-per-second must be greater than zero")
    if args.integration_key:
        print("Integration key supplied for compatibility; it will not be printed or used for provider calls.", file=sys.stderr)


def _sanitize_string(value: str) -> str:
    sanitized = BEARER_RE.sub(r"\1<redacted>", value)
    sanitized = AUTH_HEADER_RE.sub(r"\1<redacted>", sanitized)
    sanitized = USERINFO_URL_RE.sub(r"\1<redacted>\2", sanitized)
    sanitized = SECRET_QUERY_RE.sub(r"\1<redacted>", sanitized)
    return _sanitize_url_query(sanitized)


def _sanitize_url_query(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme or not parts.netloc or not parts.query:
        return value
    query = []
    changed = False
    for key, item_value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in SECRET_QUERY_NAMES:
            query.append((key, "<redacted>"))
            changed = True
        else:
            query.append((key, item_value))
    if not changed:
        return value
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _safe_error(error: Exception) -> str:
    return sanitize_for_report(str(error) or error.__class__.__name__)


if __name__ == "__main__":
    raise SystemExit(main())
