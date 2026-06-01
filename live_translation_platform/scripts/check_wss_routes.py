from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

import websockets


V1_CAPTIONS_ROUTE = "/ws/v1/lessons/{lesson_id}/captions"
V1_DIAGNOSTICS_ROUTE = "/ws/v1/lessons/{lesson_id}/diagnostics"
V1_QUESTIONS_ROUTE = "/ws/v1/lessons/{lesson_id}/questions"
V1_STUDENT_QUESTION_AUDIO_ROUTE = "/ws/v1/lessons/{lesson_id}/student-question-audio"
V1_AUDIO_INGEST_ROUTE = "/ws/v1/lessons/{lesson_id}/audio-ingest"


@dataclass
class WSSRouteReport:
    http_base_url: str
    ws_base_url: str
    health_ok: bool = False
    captions_ws_connected: bool = False
    diagnostics_ws_connected: bool = False
    questions_ws_connected: bool = False
    student_question_audio_ws_connected: bool = False
    audio_ingest_ws_connected: bool = False
    lesson_id: str | None = None
    errors: list[str] = field(default_factory=list)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check HTTP health and WS/WSS caption/question routes through a reverse proxy. "
            "Run after readiness checks. Valid scoped browser tokens are required for "
            "production v1 routes. No provider calls are made and secrets are not printed."
        ),
        epilog=(
            "Representative routes: "
            f"{V1_CAPTIONS_ROUTE}, {V1_QUESTIONS_ROUTE}. "
            "Optional route checks cover "
            f"{V1_DIAGNOSTICS_ROUTE}, {V1_STUDENT_QUESTION_AUDIO_ROUTE}, "
            f"and {V1_AUDIO_INGEST_ROUTE}."
        ),
    )
    parser.add_argument("--base-url", required=True, help="HTTP(S) base URL, for example https://python-service.example.com")
    parser.add_argument("--ws-base-url", help="WS(S) base URL, for example wss://python-service.example.com")
    parser.add_argument("--lesson-id", help="Existing lesson id. If omitted with --dev-bypass, the script tries to create a mock dev lesson.")
    parser.add_argument("--token", help="Scoped browser token for v1 WebSocket routes. The token is never printed.")
    parser.add_argument("--audio-token", help="Optional scoped teacher audio token for audio ingest checks. The token is never printed.")
    parser.add_argument("--dev-bypass", action="store_true", help="Use local development WebSocket bypass without appending a token.")
    parser.add_argument("--include-diagnostics", action="store_true", help="Also check the v1 diagnostics WebSocket route.")
    parser.add_argument("--include-audio-routes", action="store_true", help="Also check student-question-audio and audio-ingest routes; valid scoped tokens are required.")
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout in seconds for HTTP and WebSocket checks")
    args = parser.parse_args()

    http_base_url = args.base_url.rstrip("/")
    ws_base_url = (args.ws_base_url or _ws_from_http(http_base_url)).rstrip("/")
    report = WSSRouteReport(http_base_url=http_base_url, ws_base_url=ws_base_url)

    report.health_ok = _check_health(http_base_url, args.timeout, report.errors)

    lesson_id = args.lesson_id
    if not lesson_id and args.dev_bypass:
        lesson_id = _create_dev_lesson(http_base_url, args.timeout, report.errors)
    report.lesson_id = lesson_id

    if not lesson_id:
        report.errors.append("Pass --lesson-id, or use --dev-bypass against a development server that allows creating a mock lesson.")
    elif not args.token and not args.dev_bypass:
        report.errors.append("Pass --token or --dev-bypass before checking WebSocket routes.")
    else:
        try:
            asyncio.run(
                _check_websockets(
                    report,
                    lesson_id,
                    args.token,
                    args.audio_token,
                    args.dev_bypass,
                    args.include_diagnostics,
                    args.include_audio_routes,
                    args.timeout,
                )
            )
        except Exception as exc:
            report.errors.append(_safe_error(exc))

    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if report.health_ok and report.captions_ws_connected and not report.errors else 1


def _check_health(base_url: str, timeout: float, errors: list[str]) -> bool:
    try:
        payload = _fetch_json(urljoin(base_url + "/", "api/health"), timeout=timeout)
    except Exception as exc:
        errors.append(f"health check failed: {_safe_error(exc)}")
        return False
    if isinstance(payload, dict) and payload.get("status") in {"ok", "alive"}:
        return True
    errors.append("health check returned unexpected JSON")
    return False


def _create_dev_lesson(base_url: str, timeout: float, errors: list[str]) -> str | None:
    payload = {
        "title": "WSS route check",
        "mode": "mock",
        "stt_provider": "mock",
        "translation_provider": "mock",
        "target_languages": ["kk"],
    }
    try:
        response = _fetch_json(
            urljoin(base_url + "/", "api/lessons"),
            timeout=timeout,
            method="POST",
            payload=payload,
        )
    except Exception as exc:
        errors.append(f"dev lesson creation failed: {_safe_error(exc)}")
        return None
    lesson_id = response.get("lesson_id") if isinstance(response, dict) else None
    if not lesson_id:
        errors.append("dev lesson creation did not return lesson_id")
    return lesson_id


async def _check_websockets(
    report: WSSRouteReport,
    lesson_id: str,
    token: str | None,
    audio_token: str | None,
    dev_bypass: bool,
    include_diagnostics: bool,
    include_audio_routes: bool,
    timeout: float,
) -> None:
    report.captions_ws_connected = await _connect_once(
        _ws_url(report.ws_base_url, V1_CAPTIONS_ROUTE.format(lesson_id=lesson_id), token, dev_bypass),
        timeout,
        report.errors,
        "captions",
    )
    report.questions_ws_connected = await _connect_once(
        _ws_url(report.ws_base_url, V1_QUESTIONS_ROUTE.format(lesson_id=lesson_id), token, dev_bypass),
        timeout,
        report.errors,
        "questions",
    )
    if include_diagnostics:
        report.diagnostics_ws_connected = await _connect_once(
            _ws_url(report.ws_base_url, V1_DIAGNOSTICS_ROUTE.format(lesson_id=lesson_id), token, dev_bypass),
            timeout,
            report.errors,
            "diagnostics",
        )
    if include_audio_routes:
        report.student_question_audio_ws_connected = await _connect_once(
            _ws_url(report.ws_base_url, V1_STUDENT_QUESTION_AUDIO_ROUTE.format(lesson_id=lesson_id), token, dev_bypass),
            timeout,
            report.errors,
            "student-question-audio",
        )
        report.audio_ingest_ws_connected = await _connect_once(
            _ws_url(report.ws_base_url, V1_AUDIO_INGEST_ROUTE.format(lesson_id=lesson_id), audio_token or token, dev_bypass),
            timeout,
            report.errors,
            "audio-ingest",
        )


async def _connect_once(url: str, timeout: float, errors: list[str], label: str) -> bool:
    try:
        async with websockets.connect(url, open_timeout=timeout, close_timeout=min(timeout, 2)):
            return True
    except Exception as exc:
        errors.append(f"{label} websocket failed: {_safe_error(exc)}")
        return False


def _ws_url(base_url: str, path: str, token: str | None, dev_bypass: bool) -> str:
    if dev_bypass or not token:
        return base_url.rstrip("/") + path
    return base_url.rstrip("/") + path + "?" + urlencode({"token": token})


def _ws_from_http(base_url: str) -> str:
    if base_url.startswith("https://"):
        return "wss://" + base_url.removeprefix("https://")
    if base_url.startswith("http://"):
        return "ws://" + base_url.removeprefix("http://")
    return base_url


def _fetch_json(url: str, *, timeout: float, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    body = None
    headers = {"Accept": "application/json", "User-Agent": "wss-route-check/1.0"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, method=method, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _safe_error(error: Exception) -> str:
    text = error.__class__.__name__
    detail = str(error)
    if detail and "token" not in detail.lower() and "secret" not in detail.lower() and "key" not in detail.lower():
        text = f"{text}: {detail}"
    return text


if __name__ == "__main__":
    sys.exit(main())
