#!/usr/bin/env python
"""Safe preflight checks for manual real-provider E2E validation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


SECRET_KEY_RE = re.compile(
    r"(secret|token|api[_-]?key|apikey|password|authorization|credential|signature|integration[_-]?key|cookie)",
    re.IGNORECASE,
)
SECRET_QUERY_RE = re.compile(
    r"([?&](?:token|access_token|refresh_token|api_key|apikey|key|password|pwd|signature)=)[^&#\s]+",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


@dataclass
class RunOptions:
    base_url: str = "http://127.0.0.1:8000"
    integration_key_env: str = "INTEGRATION_KEY"
    run_tts_check: bool = False
    tts_provider: str = "azure"
    tts_language: str = "kk"
    tts_voice: str | None = None
    dry_run: bool = True
    allow_real_provider_calls: bool = False
    allow_zoom_call: bool = False
    create_lesson: bool = False
    lesson_id: str | None = None
    timeout: float = 10.0
    report_json: str | None = None
    max_tts_calls: int | None = None
    max_zoom_meetings: int | None = None
    max_total_provider_calls: int | None = None
    latency_threshold_ms: float | None = None
    require_quota_confirmation: bool = False
    quota_confirmed: bool = False


@dataclass
class EndpointResult:
    ok: bool
    latency_ms: float | None = None
    error: str | None = None
    payload: Any = None


@dataclass
class TTSCheck:
    ran: bool = False
    success: bool = False
    latency_ms: float | None = None
    provider: str | None = None
    language: str | None = None
    error: str | None = None


@dataclass
class Report:
    health_ready: bool = False
    providers_status: dict[str, Any] = field(default_factory=dict)
    tts_status: dict[str, Any] = field(default_factory=dict)
    lesson_created: bool = False
    lesson_id: str | None = None
    tts_check: TTSCheck = field(default_factory=TTSCheck)
    warnings: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Safe real-provider E2E preflight. By default it only checks health/provider/TTS "
            "status and does not call STT, Zoom, Translator, or TTS providers."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Application base URL.")
    parser.add_argument("--integration-key-env", default="INTEGRATION_KEY", help="Environment variable containing the backend integration key.")
    parser.add_argument("--run-tts-check", action="store_true", help="Request one TTS URL-mode synthesis; requires --allow-real-provider-calls and --no-dry-run.")
    parser.add_argument("--tts-provider", default="azure", choices=("azure", "elevenlabs", "mock"), help="TTS provider for the explicit one-call check.")
    parser.add_argument("--tts-language", default="kk", help="TTS language for the explicit one-call check.")
    parser.add_argument("--tts-voice", default="", help="Optional TTS voice id for the explicit one-call check.")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True, help="Keep real provider calls disabled; default true.")
    parser.add_argument("--allow-real-provider-calls", action="store_true", help="Required before any TTS provider call is made.")
    parser.add_argument("--allow-zoom-call", action="store_true", help="Permit creating a real Zoom-backed lesson when --create-lesson is used.")
    parser.add_argument("--create-lesson", action="store_true", help="Create one lesson through the v1 integration API when an integration key is available.")
    parser.add_argument("--lesson-id", help="Existing lesson id to use for optional checks.")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds.")
    parser.add_argument("--report-json", help="Optional path for a sanitized JSON execution report.")
    parser.add_argument("--max-tts-calls", type=int, help="Maximum planned TTS provider calls allowed for this run.")
    parser.add_argument("--max-zoom-meetings", type=int, help="Maximum planned Zoom meeting creations allowed for this run.")
    parser.add_argument("--max-total-provider-calls", type=int, help="Maximum total planned provider calls allowed for this run.")
    parser.add_argument("--latency-threshold-ms", type=float, help="Warn/fail the report when a checked endpoint exceeds this latency.")
    parser.add_argument("--require-quota-confirmation", action="store_true", help="Require --quota-confirmed before any real-provider action can run.")
    parser.add_argument("--quota-confirmed", action="store_true", help="Operator confirmation that quotas/budget were reviewed for this real-provider run.")
    return parser


def run_check(options: RunOptions) -> dict[str, Any]:
    base_url = options.base_url.rstrip("/")
    report = Report()
    integration_key = os.getenv(options.integration_key_env, "")
    endpoint_results: dict[str, EndpointResult] = {}

    health = _timed_fetch_json(base_url, "/api/health/ready", options.timeout, endpoint_results)
    providers = _timed_fetch_json(base_url, "/api/providers/status", options.timeout, endpoint_results)
    tts_status = _timed_fetch_json(base_url, "/api/tts/status", options.timeout, endpoint_results)

    report.health_ready = isinstance(health, dict) and health.get("status") == "ready"
    report.providers_status = summarize_provider_status(providers)
    report.tts_status = summarize_tts_status(tts_status)

    if not report.health_ready:
        report.warnings.append("Health readiness is not ready; inspect /api/health/ready before manual E2E.")

    planned_calls = planned_provider_calls(options)
    quota_guard = evaluate_quota_guard(options, planned_calls)
    if quota_guard["quota_guard_result"] == "fail":
        report.warnings.extend(quota_guard["errors"])

    lesson_id = options.lesson_id
    if options.create_lesson:
        if quota_guard["quota_guard_result"] == "fail":
            report.warnings.append("Lesson creation skipped because quota guard failed before provider calls.")
        elif options.dry_run:
            report.warnings.append("Lesson creation skipped because --dry-run is active; pass --no-dry-run to create one lesson.")
        elif not integration_key:
            report.warnings.append(f"Lesson creation skipped because {options.integration_key_env} is not set.")
        else:
            lesson = create_lesson(base_url, options, integration_key)
            lesson_id = lesson.get("lesson_id") if isinstance(lesson, dict) else None
            report.lesson_created = bool(lesson_id)
            if not lesson_id:
                report.warnings.append("Lesson creation failed or did not return lesson_id.")
    report.lesson_id = lesson_id

    if options.run_tts_check:
        if quota_guard["quota_guard_result"] == "fail":
            report.warnings.append("TTS check skipped because quota guard failed before provider calls.")
            report.tts_check = TTSCheck(ran=False)
        else:
            report.tts_check = run_tts_check(base_url, options, lesson_id, integration_key, report.warnings)
    else:
        report.next_steps.append("Run a manual microphone -> STT -> translation -> captions test from docs/real-provider-e2e.md.")

    report.next_steps.extend(
        [
            "Check /api/metrics/runtime for provider_errors_total, stt_disconnects_total, TTS cache metrics, and latency.",
            "Keep real-provider load small: 1 lesson, 5-10 caption clients, TTS for 1-3 students only.",
        ]
    )
    base_report = sanitize_for_report(asdict(report))
    execution_report = build_execution_report(options, endpoint_results, base_report, quota_guard=quota_guard)
    return sanitize_for_report({**execution_report, **base_report})


def create_lesson(base_url: str, options: RunOptions, integration_key: str) -> dict[str, Any]:
    payload = {
        "external_lesson_id": f"real-provider-e2e-{int(time.time())}",
        "title": "Real Provider E2E Preflight",
        "mode": "zoom" if options.allow_zoom_call else "mock",
        "stt_provider": "azure",
        "translation_provider": "azure",
        "target_languages": ["kk", "uz", "zh-Hans"],
        "create_zoom_meeting": bool(options.allow_zoom_call),
    }
    return _fetch_json(
        urljoin(base_url + "/", "api/v1/integration/lessons"),
        timeout=options.timeout,
        method="POST",
        payload=payload,
        headers={"X-Integration-Key": integration_key},
    )


def run_tts_check(base_url: str, options: RunOptions, lesson_id: str | None, integration_key: str, warnings: list[str]) -> TTSCheck:
    if options.dry_run:
        warnings.append("TTS check skipped because --dry-run is active; pass --no-dry-run and --allow-real-provider-calls deliberately.")
        return TTSCheck(ran=False)
    if not options.allow_real_provider_calls:
        warnings.append("TTS check skipped; pass --allow-real-provider-calls to spend one real provider call.")
        return TTSCheck(ran=False)
    if not integration_key:
        warnings.append(f"TTS check skipped because {options.integration_key_env} is not set.")
        return TTSCheck(ran=False)
    if not lesson_id:
        warnings.append("TTS check skipped because no lesson id is available; pass --lesson-id or --create-lesson.")
        return TTSCheck(ran=False)

    payload = {
        "text": "Real provider E2E smoke sentence.",
        "language": options.tts_language,
        "provider": options.tts_provider,
        "voice": options.tts_voice or None,
        "return_mode": "url",
    }
    started = time.perf_counter()
    try:
        response = _fetch_json(
            urljoin(base_url + "/", f"api/v1/integration/lessons/{lesson_id}/tts/synthesize"),
            timeout=options.timeout,
            method="POST",
            payload=payload,
            headers={"X-Integration-Key": integration_key},
        )
    except Exception as exc:
        return TTSCheck(ran=True, success=False, error=_safe_error(exc))
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    success = isinstance(response, dict) and bool(response.get("audio_url"))
    return TTSCheck(
        ran=True,
        success=success,
        latency_ms=latency_ms,
        provider=str(response.get("provider") or options.tts_provider) if isinstance(response, dict) else options.tts_provider,
        language=str(response.get("language") or options.tts_language) if isinstance(response, dict) else options.tts_language,
        error=None if success else "TTS synthesize did not return audio_url.",
    )


def build_execution_report(
    options: RunOptions,
    endpoint_results: dict[str, EndpointResult],
    base_report: dict[str, Any],
    quota_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    warnings = list(base_report.get("warnings") or [])
    tts_check = base_report.get("tts_check") if isinstance(base_report.get("tts_check"), dict) else {}
    sanitized_errors = [
        result.error
        for result in endpoint_results.values()
        if result.error
    ]
    sanitized_errors.extend(str(item) for item in warnings)
    tts_requested = bool(options.run_tts_check)
    real_provider_allowed = bool(options.allow_real_provider_calls and not options.dry_run)
    run_mode = _run_mode(options)
    quota_guard = quota_guard or evaluate_quota_guard(options, planned_provider_calls(options))
    latency_violations = latency_threshold_violations(options, endpoint_results)
    return {
        "run_mode": run_mode,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "base_url": options.base_url.rstrip("/"),
        "checked_endpoints": list(endpoint_results.keys()),
        "tts_check_requested": tts_requested,
        "tts_real_call_allowed": real_provider_allowed,
        "zoom_call_allowed": bool(options.allow_zoom_call and options.create_lesson and not options.dry_run),
        "status_summary": {
            "health_ready": bool(base_report.get("health_ready")),
            "providers_status_checked": "/api/providers/status" in endpoint_results,
            "tts_status_checked": "/api/tts/status" in endpoint_results,
            "lesson_created": bool(base_report.get("lesson_created")),
            "tts_check_ran": bool(tts_check.get("ran")),
            "tts_check_success": bool(tts_check.get("success")),
            "warnings_count": len(warnings) + len(latency_violations),
        },
        "latency_ms": {
            path: result.latency_ms
            for path, result in endpoint_results.items()
            if result.latency_ms is not None
        },
        "quota_guard_enabled": quota_guard["quota_guard_enabled"],
        "quota_confirmed": bool(options.quota_confirmed),
        "max_tts_calls": options.max_tts_calls,
        "max_zoom_meetings": options.max_zoom_meetings,
        "max_total_provider_calls": options.max_total_provider_calls,
        "planned_provider_calls": quota_guard["planned_provider_calls"],
        "quota_guard_result": quota_guard["quota_guard_result"],
        "latency_threshold_ms": options.latency_threshold_ms,
        "latency_threshold_violations": latency_violations,
        "sanitized_errors": sanitize_for_report(sanitized_errors),
        "final_result": _final_result(base_report, endpoint_results, quota_guard, latency_violations),
    }


def planned_provider_calls(options: RunOptions) -> dict[str, int]:
    tts_calls = int(bool(options.run_tts_check and options.allow_real_provider_calls and not options.dry_run))
    zoom_meetings = int(bool(options.create_lesson and options.allow_zoom_call and not options.dry_run))
    return {
        "tts_calls": tts_calls,
        "zoom_meetings": zoom_meetings,
        "total_provider_calls": tts_calls + zoom_meetings,
    }


def evaluate_quota_guard(options: RunOptions, planned_calls: dict[str, int]) -> dict[str, Any]:
    enabled = bool(
        options.require_quota_confirmation
        or options.max_tts_calls is not None
        or options.max_zoom_meetings is not None
        or options.max_total_provider_calls is not None
    )
    if not enabled:
        return {
            "quota_guard_enabled": False,
            "planned_provider_calls": planned_calls,
            "quota_guard_result": "not_required",
            "errors": [],
        }
    real_provider_planned = planned_calls["total_provider_calls"] > 0
    errors: list[str] = []
    if options.require_quota_confirmation and real_provider_planned and not options.quota_confirmed:
        errors.append("Quota guard blocked real-provider calls: pass --quota-confirmed after manual quota review.")
    if options.max_tts_calls is not None and planned_calls["tts_calls"] > options.max_tts_calls:
        errors.append(f"Quota guard blocked TTS calls: planned {planned_calls['tts_calls']} > max {options.max_tts_calls}.")
    if options.max_zoom_meetings is not None and planned_calls["zoom_meetings"] > options.max_zoom_meetings:
        errors.append(f"Quota guard blocked Zoom meetings: planned {planned_calls['zoom_meetings']} > max {options.max_zoom_meetings}.")
    if options.max_total_provider_calls is not None and planned_calls["total_provider_calls"] > options.max_total_provider_calls:
        errors.append(f"Quota guard blocked total provider calls: planned {planned_calls['total_provider_calls']} > max {options.max_total_provider_calls}.")
    if errors:
        result = "fail"
    elif not real_provider_planned:
        result = "not_required"
    else:
        result = "pass"
    return {
        "quota_guard_enabled": True,
        "planned_provider_calls": planned_calls,
        "quota_guard_result": result,
        "errors": errors,
    }


def latency_threshold_violations(options: RunOptions, endpoint_results: dict[str, EndpointResult]) -> list[dict[str, Any]]:
    if options.latency_threshold_ms is None:
        return []
    threshold = float(options.latency_threshold_ms)
    violations = []
    for path, result in endpoint_results.items():
        if result.latency_ms is not None and result.latency_ms > threshold:
            violations.append({"endpoint": path, "latency_ms": result.latency_ms, "threshold_ms": threshold})
    return violations


def write_report_json(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    sanitized = sanitize_for_report(report)
    target.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _run_mode(options: RunOptions) -> str:
    if options.dry_run:
        return "dry_run"
    if options.allow_real_provider_calls or options.allow_zoom_call:
        return "real_provider"
    return "preflight"


def _final_result(
    base_report: dict[str, Any],
    endpoint_results: dict[str, EndpointResult],
    quota_guard: dict[str, Any] | None = None,
    latency_violations: list[dict[str, Any]] | None = None,
) -> str:
    if not endpoint_results:
        return "not_run"
    if quota_guard and quota_guard.get("quota_guard_result") == "fail":
        return "fail"
    if latency_violations:
        return "fail"
    if any(not result.ok for result in endpoint_results.values()):
        return "fail"
    if not base_report.get("health_ready"):
        return "fail"
    tts_check = base_report.get("tts_check") if isinstance(base_report.get("tts_check"), dict) else {}
    if tts_check.get("ran") and not tts_check.get("success"):
        return "fail"
    return "pass"


def summarize_provider_status(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    summary: dict[str, Any] = {}
    for group in ("stt", "translation", "tts", "zoom"):
        section = payload.get(group)
        if not isinstance(section, dict):
            continue
        summary[group] = {}
        for name, status in section.items():
            if isinstance(status, dict):
                summary[group][name] = {
                    "ready": bool(status.get("ready")),
                    "status": status.get("status"),
                    "missing": status.get("missing", []),
                    "recommended_action": status.get("recommended_action") or status.get("recommendation"),
                }
    return summary


def summarize_tts_status(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    providers = payload.get("providers") if isinstance(payload.get("providers"), dict) else {}
    return {
        "enabled": bool(payload.get("enabled")),
        "ready": bool(payload.get("ready")),
        "provider": payload.get("provider"),
        "active_provider": payload.get("active_provider"),
        "supported_languages": payload.get("supported_languages", []),
        "audio_url_enabled": payload.get("audio_url_enabled"),
        "providers": {
            name: {
                "ready": bool(status.get("ready")) if isinstance(status, dict) else False,
                "status": status.get("status") if isinstance(status, dict) else None,
                "missing": status.get("missing", []) if isinstance(status, dict) else [],
            }
            for name, status in providers.items()
        },
    }


def _timed_fetch_json(base_url: str, path: str, timeout: float, results: dict[str, EndpointResult]) -> Any:
    started = time.perf_counter()
    try:
        payload = _fetch_json(urljoin(base_url.rstrip("/") + "/", path.lstrip("/")), timeout=timeout)
    except Exception as exc:
        results[path] = EndpointResult(ok=False, latency_ms=round((time.perf_counter() - started) * 1000, 2), error=_safe_error(exc))
        return {"error": _safe_error(exc)}
    results[path] = EndpointResult(ok=True, latency_ms=round((time.perf_counter() - started) * 1000, 2), payload=payload)
    return payload


def _safe_fetch_json(url: str, *, timeout: float) -> Any:
    try:
        return _fetch_json(url, timeout=timeout)
    except Exception as exc:
        return {"error": _safe_error(exc)}


def _fetch_json(
    url: str,
    *,
    timeout: float,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    body = None
    request_headers = {"Accept": "application/json", "User-Agent": "real-provider-e2e-check/1.0"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = Request(url, data=body, method=method, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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
        return BEARER_RE.sub(r"\1<redacted>", SECRET_QUERY_RE.sub(r"\1<redacted>", value))
    return value


def _safe_error(error: Exception) -> str:
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, URLError):
        return error.reason.__class__.__name__ if hasattr(error, "reason") else "URL error"
    return error.__class__.__name__


def options_from_args(args: argparse.Namespace) -> RunOptions:
    return RunOptions(
        base_url=args.base_url,
        integration_key_env=args.integration_key_env,
        run_tts_check=args.run_tts_check,
        tts_provider=args.tts_provider,
        tts_language=args.tts_language,
        tts_voice=args.tts_voice or None,
        dry_run=args.dry_run,
        allow_real_provider_calls=args.allow_real_provider_calls,
        allow_zoom_call=args.allow_zoom_call,
        create_lesson=args.create_lesson,
        lesson_id=args.lesson_id,
        timeout=args.timeout,
        report_json=args.report_json,
        max_tts_calls=args.max_tts_calls,
        max_zoom_meetings=args.max_zoom_meetings,
        max_total_provider_calls=args.max_total_provider_calls,
        latency_threshold_ms=args.latency_threshold_ms,
        require_quota_confirmation=args.require_quota_confirmation,
        quota_confirmed=args.quota_confirmed,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_check(options_from_args(args))
    if args.report_json:
        write_report_json(report, args.report_json)
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    warnings = report.get("warnings") or []
    return 1 if warnings and not report.get("health_ready") else 0


if __name__ == "__main__":
    raise SystemExit(main())
