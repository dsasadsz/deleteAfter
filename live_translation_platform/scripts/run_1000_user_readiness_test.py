#!/usr/bin/env python
"""Run a mock 1000-user readiness check against an already-running app."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import load_test_lessons, load_test_tts  # noqa: E402


DEFAULT_INTEGRATION_KEY_ENV = "INTEGRATION_API_KEYS"
SECRET_KEY_RE = re.compile(
    r"(secret|token|api[_-]?key|apikey|password|passwd|authorization|credential|signature|integration[_-]?key)",
    re.IGNORECASE,
)
SECRET_QUERY_RE = re.compile(
    r"([?&](?:token|access_token|refresh_token|api_key|apikey|key|password|pwd|signature)=)[^&#\s]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str
    value: Any = None


@dataclass(frozen=True)
class ResolvedIntegrationKey:
    value: str
    source: str


@dataclass(frozen=True)
class ReportPaths:
    json_path: Path
    markdown_path: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run production-like Docker load-test readiness checks for 1000 mock users."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Application base URL.")
    parser.add_argument("--students", type=int, default=1000, help="Mock students to record in the captions report.")
    parser.add_argument("--duration-seconds", type=float, default=60, help="Caption simulation duration.")
    parser.add_argument("--captions-per-second", type=float, default=3, help="Mock captions per second.")
    parser.add_argument("--tts-requests", type=int, default=1000, help="TTS URL-mode requests.")
    parser.add_argument("--tts-concurrency", type=int, default=100, help="TTS URL-mode concurrency.")
    parser.add_argument("--integration-key", default="", help="Backend integration key for v1 token creation.")
    parser.add_argument(
        "--integration-key-env",
        default=DEFAULT_INTEGRATION_KEY_ENV,
        help="Environment variable or .env key containing integration API keys.",
    )
    parser.add_argument("--env-file", default=".env", help="Local env file used only to read the integration key.")
    parser.add_argument("--output-dir", default="reports", help="Directory for Markdown and JSON reports.")
    parser.add_argument("--allow-skip-tts", action="store_true", help="Allow a WARN verdict when no integration key is available.")
    parser.add_argument("--json-only", action="store_true", help="Print only sanitized JSON to stdout.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop after the first failed stage.")
    return parser


def sanitize_url(value: str) -> str:
    return SECRET_QUERY_RE.sub(r"\1<redacted>", value)


def sanitize_for_report(value: Any, key: str | None = None) -> Any:
    if key and SECRET_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(child_key): sanitize_for_report(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [sanitize_for_report(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_for_report(item) for item in value]
    if isinstance(value, str):
        return sanitize_url(value)
    if isinstance(value, Path):
        return str(value)
    return value


def parse_env_file_value(env_file: Path | str, name: str) -> str:
    path = Path(env_file)
    if not path.exists():
        return ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key.strip() != name:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return ""


def first_csv_value(value: str) -> str:
    return next((item.strip() for item in value.split(",") if item.strip()), "")


def resolve_integration_key(
    explicit_key: str = "",
    integration_key_env: str = DEFAULT_INTEGRATION_KEY_ENV,
    env_file: Path | str = ".env",
) -> ResolvedIntegrationKey:
    if explicit_key:
        return ResolvedIntegrationKey(value=explicit_key, source="--integration-key")
    env_name = integration_key_env or DEFAULT_INTEGRATION_KEY_ENV
    from_process = first_csv_value(os.getenv(env_name, ""))
    if from_process:
        return ResolvedIntegrationKey(value=from_process, source=env_name)
    from_file = first_csv_value(parse_env_file_value(env_file, env_name))
    if from_file:
        return ResolvedIntegrationKey(value=from_file, source=env_name)
    return ResolvedIntegrationKey(value="", source=env_name)


def integration_key_missing_message(integration_key_env: str, env_file: str | Path) -> str:
    return (
        f"TTS URL-mode readiness needs an integration key. Set {integration_key_env} in {env_file} "
        f"or pass --integration-key / --integration-key-env."
    )


def check_threshold(name: str, value: Any, passed: bool, message: str) -> Check:
    return Check(name=name, status="PASS" if passed else "FAIL", message=message, value=value)


def evaluate_preflight(health: dict[str, Any], ready: dict[str, Any], metrics: dict[str, Any]) -> list[Check]:
    health_json = bool(health.get("ok") and isinstance(health.get("json"), dict))
    ready_json = bool(ready.get("ok") and isinstance(ready.get("json"), dict))
    metrics_json = bool(metrics.get("ok") and isinstance(metrics.get("json"), dict))
    health_payload = health.get("json") if isinstance(health.get("json"), dict) else {}
    ready_payload = ready.get("json") if isinstance(ready.get("json"), dict) else {}
    metrics_payload = metrics.get("json") if isinstance(metrics.get("json"), dict) else {}
    env = str(health_payload.get("env") or ready_payload.get("env") or "")
    checks = [
        check_threshold("health_json", health.get("status"), health_json, "/api/health returns JSON"),
        check_threshold("ready_json", ready.get("status"), ready_json, "/api/health/ready returns JSON"),
        check_threshold("runtime_metrics_json", metrics.get("status"), metrics_json, "/api/metrics/runtime returns JSON"),
        check_threshold("app_env_development", env, env.lower() == "development", "server is running in development load-test mode"),
        check_threshold("readiness_status", ready_payload.get("status"), ready_payload.get("status") == "ready", "readiness status is ready"),
        check_threshold("redis_enabled", metrics_payload.get("redis_enabled"), metrics_payload.get("redis_enabled") is True, "Redis is enabled"),
        check_threshold("redis_connected", metrics_payload.get("redis_connected"), metrics_payload.get("redis_connected") is True, "Redis is connected"),
        check_threshold(
            "redis_pubsub_enabled",
            metrics_payload.get("redis_pubsub_enabled"),
            metrics_payload.get("redis_pubsub_enabled") is True,
            "Redis Pub/Sub is enabled",
        ),
        check_threshold(
            "redis_rate_limit_enabled",
            metrics_payload.get("redis_rate_limit_enabled"),
            metrics_payload.get("redis_rate_limit_enabled") is True,
            "Redis rate limiting is active",
        ),
        check_threshold(
            "tts_cache_backend",
            metrics_payload.get("tts_cache_backend"),
            metrics_payload.get("tts_cache_backend") == "disk",
            "TTS shared cache backend is disk",
        ),
    ]
    return checks


def evaluate_captions_result(report: dict[str, Any]) -> list[Check]:
    metrics = report.get("runtime_metrics") if isinstance(report.get("runtime_metrics"), dict) else {}
    return [
        check_threshold(
            "captions_published",
            report.get("captions_published"),
            int(report.get("captions_published", 0) or 0) >= 180,
            "at least 180 mock captions were published",
        ),
        check_threshold(
            "captions_per_second",
            metrics.get("captions_per_second"),
            float(metrics.get("captions_per_second", 0) or 0) >= 2.5,
            "caption rate is at least 2.5/sec",
        ),
        check_threshold(
            "websocket_send_failures_total",
            metrics.get("websocket_send_failures_total"),
            int(metrics.get("websocket_send_failures_total", 0) or 0) == 0,
            "no WebSocket send failures",
        ),
        check_threshold(
            "websocket_send_timeouts_total",
            metrics.get("websocket_send_timeouts_total"),
            int(metrics.get("websocket_send_timeouts_total", 0) or 0) == 0,
            "no WebSocket send timeouts",
        ),
        check_threshold(
            "websocket_clients_dropped_total",
            metrics.get("websocket_clients_dropped_total"),
            int(metrics.get("websocket_clients_dropped_total", 0) or 0) == 0,
            "no WebSocket clients dropped",
        ),
        check_threshold(
            "redis_pubsub_messages_published_total",
            metrics.get("redis_pubsub_messages_published_total"),
            int(metrics.get("redis_pubsub_messages_published_total", 0) or 0) > 0,
            "Redis Pub/Sub published messages increased",
        ),
        check_threshold(
            "redis_pubsub_messages_received_total",
            metrics.get("redis_pubsub_messages_received_total"),
            int(metrics.get("redis_pubsub_messages_received_total", 0) or 0) > 0,
            "Redis Pub/Sub received messages increased",
        ),
        check_threshold(
            "redis_pubsub_errors_total",
            metrics.get("redis_pubsub_errors_total"),
            int(metrics.get("redis_pubsub_errors_total", 0) or 0) == 0,
            "Redis Pub/Sub errors stayed at zero",
        ),
    ]


def evaluate_tts_result(report: dict[str, Any], expected_requests: int) -> list[Check]:
    provider_calls_delta = int(report.get("provider_calls_after", 0) or 0) - int(report.get("provider_calls_before", 0) or 0)
    expected_saved = max(0, expected_requests - 10)
    return [
        check_threshold("tts_success", report.get("success"), int(report.get("success", 0) or 0) == expected_requests, "all TTS requests succeeded"),
        check_threshold("tts_failed", report.get("failed"), int(report.get("failed", 0) or 0) == 0, "no TTS requests failed"),
        check_threshold(
            "tts_audio_url_success",
            report.get("audio_url_success"),
            int(report.get("audio_url_success", 0) or 0) == expected_requests,
            "all signed audio URLs were fetched",
        ),
        check_threshold(
            "tts_audio_url_failed",
            report.get("audio_url_failed"),
            int(report.get("audio_url_failed", 0) or 0) == 0,
            "no signed audio URL fetches failed",
        ),
        check_threshold(
            "tts_auth_401_count",
            report.get("auth_401_count"),
            int(report.get("auth_401_count", 0) or 0) == 0,
            "no TTS auth 401 responses",
        ),
        check_threshold(
            "tts_provider_calls_delta",
            provider_calls_delta,
            provider_calls_delta <= 3,
            "same-caption URL mode caused no more than 3 provider calls",
        ),
        check_threshold(
            "tts_provider_calls_saved",
            report.get("provider_calls_saved"),
            int(report.get("provider_calls_saved", 0) or 0) >= expected_saved,
            f"provider calls saved is at least {expected_saved}",
        ),
        check_threshold(
            "tts_cache_hits",
            report.get("cache_hits"),
            int(report.get("cache_hits", 0) or 0) >= expected_saved,
            f"TTS cache hits are at least {expected_saved}",
        ),
        check_threshold(
            "tts_cache_misses",
            report.get("cache_misses"),
            int(report.get("cache_misses", 0) or 0) <= 3,
            "TTS cache misses are no more than 3",
        ),
    ]


def overall_verdict(checks: list[Check] | list[dict[str, Any]]) -> str:
    statuses = {check.status if isinstance(check, Check) else str(check.get("status")) for check in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def fetch_json(base_url: str, path: str, timeout: float = 15.0) -> dict[str, Any]:
    request = Request(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        headers={"Accept": "application/json", "User-Agent": "1000-user-readiness/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {"ok": False, "status": response.status, "error": "InvalidJSON"}
            return {"ok": 200 <= response.status < 300, "status": response.status, "json": payload}
    except HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.__class__.__name__}
    except (TimeoutError, URLError, OSError) as exc:
        return {"ok": False, "error": exc.__class__.__name__}


def run_load_test_script(run_func: Callable[[argparse.Namespace], int], args: argparse.Namespace) -> tuple[int, dict[str, Any], str]:
    stream = io.StringIO()
    system_exit_message = ""
    try:
        with contextlib.redirect_stdout(stream):
            exit_code = int(run_func(args))
    except SystemExit as exc:
        if isinstance(exc.code, int):
            exit_code = exc.code
        else:
            exit_code = 1
            system_exit_message = str(exc.code or "")
    output = stream.getvalue().strip()
    try:
        payload = json.loads(output) if output else {"error": system_exit_message} if system_exit_message else {}
    except json.JSONDecodeError:
        payload = {"error": output or "script did not return JSON"}
    return exit_code, payload, output


def run_captions_stage(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    lesson_args = load_test_lessons.build_parser().parse_args(
        [
            "--base-url",
            args.base_url,
            "--lessons",
            "1",
            "--students",
            str(args.students),
            "--duration-seconds",
            str(args.duration_seconds),
            "--mock",
            "--simulate-captions",
            "--captions-per-second",
            str(args.captions_per_second),
            "--report-system",
        ]
    )
    exit_code, report, _output = run_load_test_script(load_test_lessons.run, lesson_args)
    return exit_code, report


def run_tts_stage(args: argparse.Namespace, integration_key: str) -> tuple[int, dict[str, Any]]:
    tts_args = load_test_tts.build_parser().parse_args(
        [
            "--base-url",
            args.base_url,
            "--requests",
            str(args.tts_requests),
            "--concurrency",
            str(args.tts_concurrency),
            "--return-mode",
            "url",
            "--same-caption",
            "--provider",
            "mock",
            "--disable-rate-limit-for-load-test",
            "--use-v1",
            "--integration-key",
            integration_key,
        ]
    )
    exit_code, report, _output = run_load_test_script(load_test_tts.run, tts_args)
    return exit_code, report


def collect_environment_summary(base_url: str, health: dict[str, Any], ready: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    health_payload = health.get("json") if isinstance(health.get("json"), dict) else {}
    ready_payload = ready.get("json") if isinstance(ready.get("json"), dict) else {}
    metrics_payload = metrics.get("json") if isinstance(metrics.get("json"), dict) else {}
    return {
        "base_url": base_url,
        "app_env": health_payload.get("env") or ready_payload.get("env"),
        "database_type": health_payload.get("database_type") or ready_payload.get("database_type"),
        "redis_enabled": metrics_payload.get("redis_enabled"),
        "redis_connected": metrics_payload.get("redis_connected"),
        "redis_pubsub_enabled": metrics_payload.get("redis_pubsub_enabled"),
        "redis_rate_limit_enabled": metrics_payload.get("redis_rate_limit_enabled"),
        "tts_cache_backend": metrics_payload.get("tts_cache_backend"),
    }


def as_check_dicts(checks: list[Check]) -> list[dict[str, Any]]:
    return [asdict(check) for check in checks]


def write_reports(report: dict[str, Any], output_dir: Path | str) -> ReportPaths:
    sanitized = sanitize_for_report(report)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = directory / f"1000_user_readiness_{timestamp}.json"
    markdown_path = directory / f"1000_user_readiness_{timestamp}.md"
    json_path.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown_report(sanitized), encoding="utf-8")
    return ReportPaths(json_path=json_path, markdown_path=markdown_path)


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# 1000-User Mock Readiness Report",
        "",
        f"Overall Verdict: {report.get('verdict', 'UNKNOWN')}",
        f"Generated At: {report.get('generated_at', '')}",
        f"Base URL: {report.get('base_url', '')}",
        "",
        "## Environment Summary",
        "",
    ]
    for key, value in (report.get("environment_summary") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Checks", ""])
    for check in report.get("checks", []):
        lines.append(f"- {check.get('status')} `{check.get('name')}`: {check.get('message')} (value: `{check.get('value')}`)")
    lines.extend(["", "## Captions And Redis Pub/Sub", "", "```json"])
    lines.append(json.dumps(report.get("captions", {}), indent=2, ensure_ascii=False, sort_keys=True))
    lines.extend(["```", "", "## TTS URL Cache", "", "```json"])
    lines.append(json.dumps(report.get("tts", {}), indent=2, ensure_ascii=False, sort_keys=True))
    lines.extend(["```", "", "## Final Metrics Snapshot", "", "```json"])
    lines.append(json.dumps(report.get("final_metrics", {}), indent=2, ensure_ascii=False, sort_keys=True))
    lines.extend(["```", "", "## Limitations", ""])
    for limitation in report.get("limitations", []):
        lines.append(f"- {limitation}")
    lines.extend(["", "## Next Recommended Step", "", str(report.get("next_recommended_step", "")), ""])
    return "\n".join(lines)


def run_plan(args: argparse.Namespace) -> dict[str, Any]:
    health = fetch_json(args.base_url, "/api/health")
    ready = fetch_json(args.base_url, "/api/health/ready")
    metrics = fetch_json(args.base_url, "/api/metrics/runtime")
    preflight_checks = evaluate_preflight(health, ready, metrics)
    all_checks: list[Check] = list(preflight_checks)
    captions_report: dict[str, Any] = {}
    tts_report: dict[str, Any] = {}
    tts_skip_reason = ""

    if args.fail_fast and overall_verdict(preflight_checks) == "FAIL":
        final_metrics = metrics.get("json") if isinstance(metrics.get("json"), dict) else {}
    else:
        captions_exit, captions_report = run_captions_stage(args)
        if captions_exit != 0:
            all_checks.append(Check("captions_script_exit", "FAIL", "load_test_lessons.py exited non-zero", captions_exit))
        all_checks.extend(evaluate_captions_result(captions_report))
        if not (args.fail_fast and overall_verdict(all_checks) == "FAIL"):
            resolved = resolve_integration_key(args.integration_key, args.integration_key_env, args.env_file)
            if not resolved.value:
                tts_skip_reason = integration_key_missing_message(args.integration_key_env, args.env_file)
                status = "WARN" if args.allow_skip_tts else "FAIL"
                all_checks.append(Check("tts_integration_key", status, tts_skip_reason, "missing"))
                tts_report = {"skipped": True, "reason": tts_skip_reason}
            else:
                tts_exit, tts_report = run_tts_stage(args, resolved.value)
                if tts_exit != 0:
                    all_checks.append(Check("tts_script_exit", "FAIL", "load_test_tts.py exited non-zero", tts_exit))
                all_checks.extend(evaluate_tts_result(tts_report, expected_requests=args.tts_requests))
        final_payload = fetch_json(args.base_url, "/api/metrics/runtime")
        final_metrics = final_payload.get("json") if isinstance(final_payload.get("json"), dict) else final_payload

    verdict = overall_verdict(all_checks)
    return {
        "verdict": verdict,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "environment_summary": collect_environment_summary(args.base_url, health, ready, metrics),
        "checks": as_check_dicts(all_checks),
        "preflight": {
            "health": health,
            "ready": ready,
            "runtime_metrics": metrics,
        },
        "captions": captions_report,
        "redis_pubsub": {
            "published_total": (captions_report.get("runtime_metrics") or {}).get("redis_pubsub_messages_published_total"),
            "received_total": (captions_report.get("runtime_metrics") or {}).get("redis_pubsub_messages_received_total"),
            "errors_total": (captions_report.get("runtime_metrics") or {}).get("redis_pubsub_errors_total"),
            "latency_ms_avg": (captions_report.get("runtime_metrics") or {}).get("redis_pubsub_latency_ms_avg"),
        },
        "tts": tts_report,
        "tts_skip_reason": tts_skip_reason,
        "final_metrics": final_metrics,
        "limitations": [
            "mock providers only",
            "not real Azure/ElevenLabs/Zoom proof",
            "not browser/device network proof",
            "caption simulation publishes mock events; it does not prove every real browser receives audio playback",
        ],
        "next_recommended_step": "real-provider small E2E using docs/REAL_PROVIDER_E2E_TEST.md",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_plan(args)
    paths = write_reports(report, args.output_dir)
    report["report_paths"] = {"json": str(paths.json_path), "markdown": str(paths.markdown_path)}
    sanitized = sanitize_for_report(report)
    if not args.json_only:
        print(f"1000-user mock readiness verdict: {sanitized['verdict']}")
        print(f"JSON report: {paths.json_path}")
        print(f"Markdown report: {paths.markdown_path}")
    print(json.dumps(sanitized, indent=2, ensure_ascii=False, sort_keys=True))
    return 1 if sanitized.get("verdict") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
