#!/usr/bin/env python
"""Run a small manual-assisted real-provider smoke test.

This script intentionally performs only a tiny provider check:
one lesson, one short TTS URL-mode synthesis, one audio URL download,
and an operator-assisted teacher microphone step.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


SECRET_KEY_RE = re.compile(
    r"(secret|token|api[_-]?key|apikey|password|passwd|authorization|credential|signature|zak|start_url|join_url)",
    re.IGNORECASE,
)
SECRET_QUERY_RE = re.compile(
    r"([?&](?:token|access_token|refresh_token|api_key|apikey|key|password|pwd|signature|zak)=)[^&#\s]+",
    re.IGNORECASE,
)

DEFAULT_TTS_TEXT = "Hello from the real provider smoke test."
SUPPORTED_SMOKE_LANGUAGES = ("kk", "ru", "uz", "zh-Hans")
SUPPORTED_TTS_PROVIDERS = ("azure", "mock", "elevenlabs")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str
    value: Any = None


@dataclass(frozen=True)
class ReportPaths:
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class SmokeOptions:
    base_url: str
    tts_provider: str
    language: str
    reports_dir: Path
    manual: bool
    timeout: float
    stt_provider: str = ""
    translation_provider: str = "azure"
    tts_text: str = DEFAULT_TTS_TEXT


class SmokeHttpClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_json(self, path: str) -> dict[str, Any]:
        return self._request_json("GET", path)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", path, payload)

    def get_bytes(self, url_or_path: str) -> dict[str, Any]:
        request = Request(
            self._url(url_or_path),
            headers={"Accept": "audio/*,*/*", "User-Agent": "real-provider-smoke/1.0"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return {
                    "ok": 200 <= response.status < 300,
                    "status": response.status,
                    "bytes": response.read(),
                    "content_type": response.headers.get("content-type"),
                }
        except HTTPError as exc:
            return {"ok": False, "status": exc.code, "error": exc.__class__.__name__}
        except (TimeoutError, URLError, OSError) as exc:
            return {"ok": False, "error": exc.__class__.__name__}

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "User-Agent": "real-provider-smoke/1.0"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self._url(path), data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                try:
                    decoded = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    return {"ok": False, "status": response.status, "error": "InvalidJSON"}
                return {"ok": 200 <= response.status < 300, "status": response.status, "json": decoded}
        except HTTPError as exc:
            details: dict[str, Any] = {"ok": False, "status": exc.code, "error": exc.__class__.__name__}
            try:
                details["body"] = json.loads(exc.read().decode("utf-8"))
            except Exception:
                pass
            return details
        except (TimeoutError, URLError, OSError) as exc:
            return {"ok": False, "error": exc.__class__.__name__}

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return urljoin(self.base_url + "/", path_or_url.lstrip("/"))


def sanitize_url(value: str) -> str:
    return SECRET_QUERY_RE.sub(r"\1<redacted>", value)


def _mask_identifier(value: Any) -> str:
    text = str(value)
    if len(text) <= 4:
        return "[masked]"
    return f"[masked:{text[-4:]}]"


def sanitize_for_report(value: Any, key: str | None = None) -> Any:
    if key and "meeting_id" in key.lower():
        return _mask_identifier(value)
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
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a tiny real-provider smoke test without 1000-user load: readiness, provider status, "
            "one lesson, one TTS URL synthesis, one audio download, then a manual teacher mic step."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Application base URL.")
    parser.add_argument("--tts-provider", choices=SUPPORTED_TTS_PROVIDERS, default="azure", help="TTS provider for the single synthesize call.")
    parser.add_argument("--language", choices=SUPPORTED_SMOKE_LANGUAGES, default="kk", help="TTS target language.")
    parser.add_argument("--reports-dir", default="reports", help="Directory for JSON and Markdown reports.")
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout in seconds.")
    parser.add_argument("--stt-provider", default=os.getenv("STT_PROVIDER", "azure"), help="Expected STT provider; defaults to STT_PROVIDER or azure.")
    parser.add_argument("--translation-provider", default="azure", help="Expected translation provider. Keep azure for real-provider smoke.")
    parser.add_argument("--tts-text", default=DEFAULT_TTS_TEXT, help="Short text used for the single TTS synthesis call.")
    parser.add_argument("--no-manual", action="store_true", help="Do not pause for the teacher mic manual step; still records instructions.")
    parser.add_argument("--json-only", action="store_true", help="Print only sanitized JSON to stdout.")
    return parser


def status_check(name: str, response: dict[str, Any], expected_status: str | None = None) -> Check:
    payload = response.get("json") if isinstance(response.get("json"), dict) else {}
    if not response.get("ok", 200 <= int(response.get("status", 0) or 0) < 300):
        return Check(name, "FAIL", f"HTTP check failed for {name}", response.get("status") or response.get("error"))
    if expected_status and payload.get("status") != expected_status:
        return Check(name, "FAIL", f"{name} status is not {expected_status}", payload.get("status"))
    return Check(name, "PASS", f"{name} responded successfully", response.get("status"))


def provider_ready_check(kind: str, provider: str, providers_status: dict[str, Any], tts_status: dict[str, Any] | None = None) -> Check:
    section = providers_status.get(kind)
    payload = section.get(provider) if isinstance(section, dict) else None
    tts_payload = None
    if kind == "tts" and isinstance(tts_status, dict):
        tts_providers = tts_status.get("providers")
        if isinstance(tts_providers, dict):
            tts_payload = tts_providers.get(provider)
    ready = bool(isinstance(payload, dict) and payload.get("ready"))
    if kind == "tts" and tts_payload is not None:
        ready = ready and bool(isinstance(tts_payload, dict) and tts_payload.get("ready"))
    if ready:
        return Check(f"{kind}_{provider}_ready", "PASS", f"{kind} provider {provider} is ready")
    return Check(
        f"{kind}_{provider}_ready",
        "FAIL",
        f"{kind} provider {provider} is not ready",
        {"provider_status": payload, "tts_status": tts_payload},
    )


def create_smoke_lesson(client: Any, options: SmokeOptions) -> dict[str, Any]:
    payload = {
        "title": f"Real provider smoke {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "mode": "mock",
        "audio_source": "browser_ws",
        "stt_provider": options.stt_provider or "azure",
        "translation_provider": options.translation_provider,
        "target_languages": [options.language],
        "glossary_enabled": False,
    }
    return client.post_json("/api/lessons", payload)


def synthesize_tts_url(client: Any, lesson_id: str, options: SmokeOptions) -> dict[str, Any]:
    payload = {
        "text": options.tts_text,
        "language": options.language,
        "provider": options.tts_provider,
        "return_mode": "url",
    }
    return client.post_json(f"/api/lessons/{lesson_id}/tts/synthesize", payload)


def manual_instructions(base_url: str, lesson_id: str, language: str) -> list[str]:
    teacher_url = urljoin(base_url.rstrip("/") + "/", f"teacher/{lesson_id}")
    return [
        f"Open teacher page: {teacher_url}",
        "Allow microphone access and start teacher mic streaming.",
        f"Say one short phrase clearly, then wait for final captions/translations for {language}.",
        "Confirm teacher mic chunks grow and no provider errors appear.",
        "Press Enter in this terminal after the manual check to collect runtime metrics and diagnostics.",
    ]


def maybe_run_manual_step(options: SmokeOptions, lesson_id: str) -> list[str]:
    instructions = manual_instructions(options.base_url, lesson_id, options.language)
    if not options.manual:
        return instructions
    print("\nManual teacher mic step:")
    for index, instruction in enumerate(instructions, 1):
        print(f"{index}. {sanitize_for_report(instruction)}")
    if sys.stdin.isatty():
        input("\nPress Enter after the teacher mic check...")
    else:
        print("\nNon-interactive stdin detected; continuing without waiting.")
    return instructions


def run_smoke_test(options: SmokeOptions, client: Any | None = None) -> dict[str, Any]:
    client = client or SmokeHttpClient(options.base_url, timeout=options.timeout)
    checks: list[Check] = []

    health_ready = client.get_json("/api/health/ready")
    providers_response = client.get_json("/api/providers/status?live=true")
    tts_status_response = client.get_json("/api/tts/status")
    checks.append(status_check("health_ready", health_ready, expected_status="ready"))
    checks.append(status_check("providers_status_live", providers_response))
    checks.append(status_check("tts_status", tts_status_response))

    providers_payload = providers_response.get("json") if isinstance(providers_response.get("json"), dict) else {}
    tts_status_payload = tts_status_response.get("json") if isinstance(tts_status_response.get("json"), dict) else {}
    checks.append(provider_ready_check("stt", options.stt_provider or "azure", providers_payload))
    checks.append(provider_ready_check("translation", options.translation_provider, providers_payload))
    checks.append(provider_ready_check("tts", options.tts_provider, providers_payload, tts_status_payload))

    lesson_response = create_smoke_lesson(client, options)
    lesson_payload = lesson_response.get("json") if isinstance(lesson_response.get("json"), dict) else {}
    lesson_id = str(lesson_payload.get("lesson_id") or "")
    if lesson_response.get("ok", 200 <= int(lesson_response.get("status", 0) or 0) < 300) and lesson_id:
        checks.append(Check("lesson_created", "PASS", "one smoke lesson was created", lesson_id))
    else:
        checks.append(Check("lesson_created", "FAIL", "could not create the smoke lesson", lesson_response))

    tts_response: dict[str, Any] = {}
    audio_fetch: dict[str, Any] = {}
    tts_payload: dict[str, Any] = {}
    if lesson_id:
        tts_response = synthesize_tts_url(client, lesson_id, options)
        tts_payload = tts_response.get("json") if isinstance(tts_response.get("json"), dict) else {}
        audio_url = str(tts_payload.get("audio_url") or "")
        if tts_response.get("ok", 200 <= int(tts_response.get("status", 0) or 0) < 300) and audio_url:
            checks.append(Check("tts_synthesize_url", "PASS", "single TTS URL-mode synthesize returned audio_url"))
            audio_fetch = client.get_bytes(audio_url)
            audio_bytes = audio_fetch.get("bytes") if isinstance(audio_fetch.get("bytes"), bytes) else b""
            if audio_fetch.get("ok", 200 <= int(audio_fetch.get("status", 0) or 0) < 300) and len(audio_bytes) > 0:
                checks.append(Check("tts_audio_download", "PASS", "downloaded non-empty TTS audio bytes", len(audio_bytes)))
            else:
                checks.append(Check("tts_audio_download", "FAIL", "TTS audio download returned no bytes", audio_fetch))
        else:
            checks.append(Check("tts_synthesize_url", "FAIL", "single TTS URL-mode synthesize failed", tts_response))

    manual_steps = manual_instructions(options.base_url, lesson_id or "<lesson_id>", options.language)
    if lesson_id:
        manual_steps = maybe_run_manual_step(options, lesson_id)
        checks.append(Check("teacher_mic_manual", "MANUAL", "operator must verify teacher mic/STT captions manually", manual_steps[0]))

    runtime_after_manual = client.get_json("/api/metrics/runtime")
    diagnostics_after_manual = client.get_json(f"/api/lessons/{lesson_id}/diagnostics") if lesson_id else {}

    return sanitize_for_report(
        {
            "verdict": overall_verdict(checks),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "base_url": options.base_url.rstrip("/"),
            "lesson_id": lesson_id,
            "selected_providers": {
                "stt_provider": options.stt_provider or "azure",
                "translation_provider": options.translation_provider,
                "tts_provider": options.tts_provider,
            },
            "language": options.language,
            "checks": [asdict(check) for check in checks],
            "preflight": {
                "health_ready": health_ready,
                "providers_status_live": providers_response,
                "tts_status": tts_status_response,
            },
            "lesson": lesson_response,
            "tts": {
                "provider": tts_payload.get("provider") or options.tts_provider,
                "language": tts_payload.get("language") or options.language,
                "audio_url": tts_payload.get("audio_url"),
                "audio_bytes": len(audio_fetch.get("bytes", b"")) if isinstance(audio_fetch.get("bytes"), bytes) else 0,
                "content_type": audio_fetch.get("content_type"),
                "synthesize_response": tts_response,
                "audio_fetch": audio_fetch,
            },
            "manual_steps": manual_steps,
            "runtime_after_manual": runtime_after_manual,
            "diagnostics_after_manual": diagnostics_after_manual,
            "limitations": [
                "No 1000-user real-provider load test is run.",
                "Only one lesson and one short TTS synthesis are performed.",
                "Teacher microphone/STT verification remains manual-assisted.",
            ],
        }
    )


def overall_verdict(checks: list[Check] | list[dict[str, Any]]) -> str:
    statuses = {check.status if isinstance(check, Check) else str(check.get("status")) for check in checks}
    if "FAIL" in statuses:
        return "FAIL"
    if "MANUAL" in statuses:
        return "MANUAL_REQUIRED"
    if "WARN" in statuses:
        return "WARN"
    return "PASS"


def write_reports(report: dict[str, Any], output_dir: Path | str) -> ReportPaths:
    sanitized = sanitize_for_report(report)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = directory / f"real_provider_smoke_{timestamp}.json"
    markdown_path = directory / f"real_provider_smoke_{timestamp}.md"
    json_path.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown_report(sanitized), encoding="utf-8")
    return ReportPaths(json_path=json_path, markdown_path=markdown_path)


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Real Provider Smoke Test",
        "",
        f"Overall Verdict: {report.get('verdict', 'UNKNOWN')}",
        f"Generated At: {report.get('generated_at', '')}",
        f"Base URL: {report.get('base_url', '')}",
        f"Lesson ID: {report.get('lesson_id', '')}",
        f"Language: {report.get('language', '')}",
        "",
        "## Selected Providers",
        "",
    ]
    for key, value in (report.get("selected_providers") or {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Checks", ""])
    for check in report.get("checks", []):
        lines.append(f"- {check.get('status')} `{check.get('name')}`: {check.get('message')} (value: `{check.get('value')}`)")
    lines.extend(["", "## TTS", "", "```json"])
    lines.append(json.dumps(report.get("tts", {}), indent=2, ensure_ascii=False, sort_keys=True))
    lines.extend(["```", "", "## Manual Teacher Mic Step", ""])
    for instruction in report.get("manual_steps", []):
        lines.append(f"- {instruction}")
    lines.extend(["", "## Runtime Metrics After Manual Step", "", "```json"])
    lines.append(json.dumps(report.get("runtime_after_manual", {}), indent=2, ensure_ascii=False, sort_keys=True))
    lines.extend(["```", "", "## Lesson Diagnostics After Manual Step", "", "```json"])
    lines.append(json.dumps(report.get("diagnostics_after_manual", {}), indent=2, ensure_ascii=False, sort_keys=True))
    lines.extend(["```", "", "## Limitations", ""])
    for limitation in report.get("limitations", []):
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)


def options_from_args(args: argparse.Namespace) -> SmokeOptions:
    return SmokeOptions(
        base_url=args.base_url,
        tts_provider=args.tts_provider,
        language=args.language,
        reports_dir=Path(args.reports_dir),
        manual=not args.no_manual,
        timeout=args.timeout,
        stt_provider=args.stt_provider,
        translation_provider=args.translation_provider,
        tts_text=args.tts_text,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = options_from_args(args)
    report = run_smoke_test(options)
    paths = write_reports(report, options.reports_dir)
    report["report_paths"] = {"json": str(paths.json_path), "markdown": str(paths.markdown_path)}
    sanitized = sanitize_for_report(report)
    if not args.json_only:
        print(f"Real-provider smoke verdict: {sanitized['verdict']}")
        print(f"JSON report: {paths.json_path}")
        print(f"Markdown report: {paths.markdown_path}")
    print(json.dumps(sanitized, indent=2, ensure_ascii=False, sort_keys=True))
    return 1 if sanitized.get("verdict") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
