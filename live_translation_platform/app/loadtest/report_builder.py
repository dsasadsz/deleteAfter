from __future__ import annotations

import html
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


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


def percentile(values: list[float | int], percent: float) -> float | None:
    clean = sorted(float(value) for value in values if _finite(value))
    if not clean:
        return None
    if len(clean) == 1:
        return round(clean[0], 2)
    rank = (len(clean) - 1) * (percent / 100.0)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(clean[int(rank)], 2)
    weight = rank - lower
    return round(clean[lower] * (1 - weight) + clean[upper] * weight, 2)


def percentile_summary(values: list[float | int]) -> dict[str, float | int | None]:
    clean = [float(value) for value in values if _finite(value)]
    return {
        "count": len(clean),
        "p50": percentile(clean, 50),
        "p95": percentile(clean, 95),
        "p99": percentile(clean, 99),
    }


def build_local_load_test_report(run_data: dict[str, Any]) -> dict[str, Any]:
    request = run_data.get("request") or {}
    expected_students = int(request.get("sessions", 0) or 0) * int(request.get("students_per_session", 0) or 0)
    students = list(run_data.get("students") or [])
    connected_students = sum(1 for student in students if student.get("connected"))
    caption_events = list(run_data.get("caption_events") or [])
    tts_events = list(run_data.get("tts_events") or [])
    receive_latencies = [_number(event.get("student_receive_latency_ms")) for event in caption_events]
    receive_latencies = [value for value in receive_latencies if value is not None]
    stt_latencies = [_number(event.get("stt_latency_ms")) for event in caption_events]
    translation_latencies = [_number(event.get("translation_latency_ms")) for event in caption_events]
    tts_hit_latencies = [_number(event.get("latency_ms")) for event in tts_events if event.get("cache_status") == "hit"]
    tts_miss_latencies = [_number(event.get("latency_ms")) for event in tts_events if event.get("cache_status") == "miss"]
    tts_hits = sum(1 for event in tts_events if event.get("cache_status") == "hit")
    provider_errors = list(run_data.get("provider_errors") or [])
    sessions = list(run_data.get("sessions") or [])
    disconnected = max(0, expected_students - connected_students)
    dropped_chunks = int(run_data.get("dropped_chunks", 0) or 0)
    infrastructure_verdict = _infrastructure_verdict(
        expected_students=expected_students,
        connected_students=connected_students,
        sessions=sessions,
        provider_errors=provider_errors,
        receive_p95=percentile(receive_latencies, 95),
        caption_events=caption_events,
        disconnected=disconnected,
        dropped_chunks=dropped_chunks,
    )
    model_latency_verdict = _latency_verdict(stt_latencies, translation_latencies, tts_hit_latencies, tts_miss_latencies)
    quality_verdict = run_data.get("quality_verdict") or ("DEGRADED" if request.get("mode") in {"real_pipeline", "full"} else "PASS")
    report = {
        "run_id": run_data.get("run_id"),
        "status": run_data.get("status"),
        "request": request,
        "summary": {
            "sessions": len(sessions),
            "students_expected": expected_students,
            "students_connected": connected_students,
            "connected_ratio": round(connected_students / max(1, expected_students), 4),
            "disconnects": disconnected,
            "caption_events": len(caption_events),
            "provider_errors": len(provider_errors),
            "dropped_chunks": dropped_chunks,
        },
        "latency": {
            "stt_latency_ms": percentile_summary([value for value in stt_latencies if value is not None]),
            "translation_latency_ms": percentile_summary([value for value in translation_latencies if value is not None]),
            "student_receive_latency_ms": percentile_summary(receive_latencies),
            "tts_hit_latency_ms": percentile_summary([value for value in tts_hit_latencies if value is not None]),
            "tts_miss_latency_ms": percentile_summary([value for value in tts_miss_latencies if value is not None]),
        },
        "tts": {
            "events": len(tts_events),
            "cache_hits": tts_hits,
            "cache_misses": len(tts_events) - tts_hits,
            "cache_hit_ratio": tts_hits / len(tts_events) if tts_events else 0,
        },
        "quality": run_data.get("quality") or {},
        "audio": run_data.get("audio") or {},
        "metric_snapshots": run_data.get("metric_snapshots") or [],
        "logs": run_data.get("logs") or [],
        "errors": provider_errors,
        "infrastructure_verdict": infrastructure_verdict,
        "model_latency_verdict": model_latency_verdict,
        "quality_verdict": quality_verdict,
    }
    report["overall_verdict"] = _overall_verdict(
        infrastructure_verdict,
        model_latency_verdict,
        quality_verdict,
        mode=str(request.get("mode") or "light"),
    )
    return sanitize_for_report(report)


def render_markdown_report(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    latency = report.get("latency") or {}
    lines = [
        f"# Local Virtual Lesson Load Test {report.get('run_id')}",
        "",
        f"- Status: {report.get('status')}",
        f"- Overall verdict: {report.get('overall_verdict')}",
        f"- Infrastructure verdict: {report.get('infrastructure_verdict')}",
        f"- Model latency verdict: {report.get('model_latency_verdict')}",
        f"- Quality verdict: {report.get('quality_verdict')}",
        f"- Connected students: {summary.get('students_connected')}/{summary.get('students_expected')}",
        f"- Caption events: {summary.get('caption_events')}",
        f"- Provider errors: {summary.get('provider_errors')}",
        "",
        "## Latency",
    ]
    for name, values in latency.items():
        lines.append(f"- {name}: p50={values.get('p50')} p95={values.get('p95')} p99={values.get('p99')} count={values.get('count')}")
    audio = ((report.get("audio") or {}).get("normalized") or {})
    if audio:
        lines.extend(
            [
                "",
                "## Audio",
                f"- Duration seconds: {audio.get('duration_seconds')}",
                f"- Sample rate: {audio.get('sample_rate')}",
                f"- Channels: {audio.get('channels')}",
                f"- Format: {audio.get('format')}",
                f"- Decoder: {audio.get('decoder')}",
            ]
        )
    return "\n".join(lines) + "\n"


def render_html_report(report: dict[str, Any]) -> str:
    markdown = render_markdown_report(report)
    escaped = html.escape(markdown)
    return f"<!doctype html><html><head><meta charset=\"utf-8\"><title>Local Load Test {html.escape(str(report.get('run_id')))}</title></head><body><pre>{escaped}</pre></body></html>"


def write_json_report(report: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(sanitize_for_report(report), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return target


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


def _infrastructure_verdict(
    *,
    expected_students: int,
    connected_students: int,
    sessions: list[dict[str, Any]],
    provider_errors: list[Any],
    receive_p95: float | None,
    caption_events: list[dict[str, Any]],
    disconnected: int,
    dropped_chunks: int,
) -> str:
    if expected_students and connected_students < math.ceil(expected_students * 0.95):
        return "FAIL"
    if expected_students and disconnected > expected_students * 0.05:
        return "FAIL"
    if not all(session.get("status") in {"completed", "running", "stopped", "cancelled"} for session in sessions):
        return "FAIL"
    if provider_errors:
        return "FAIL"
    if sessions and not caption_events:
        return "FAIL"
    if receive_p95 is not None and receive_p95 > 1500:
        return "DEGRADED"
    if dropped_chunks > 0:
        return "DEGRADED"
    return "PASS"


def _latency_verdict(
    stt_latencies: list[float | None],
    translation_latencies: list[float | None],
    tts_hit_latencies: list[float | None],
    tts_miss_latencies: list[float | None],
) -> str:
    tts_hit_p95 = percentile([value for value in tts_hit_latencies if value is not None], 95)
    if tts_hit_p95 is not None and tts_hit_p95 > 100:
        return "FAIL"
    provider_values = [value for value in [*stt_latencies, *translation_latencies] if value is not None]
    provider_p95 = percentile(provider_values, 95)
    if provider_p95 is not None and provider_p95 > 3000:
        return "DEGRADED"
    tts_miss_p95 = percentile([value for value in tts_miss_latencies if value is not None], 95)
    if tts_miss_p95 is not None and tts_miss_p95 > 3000:
        return "DEGRADED"
    return "PASS"


def _overall_verdict(infrastructure: str, model_latency: str, quality: str, *, mode: str) -> str:
    verdicts = [infrastructure, model_latency]
    if mode in {"real_pipeline", "full"}:
        verdicts.append(quality)
    if "FAIL" in verdicts:
        return "FAIL"
    if "DEGRADED" in verdicts:
        return "DEGRADED"
    return "PASS"


def _finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def _number(value: Any) -> float | None:
    return float(value) if _finite(value) else None


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
