#!/usr/bin/env python
"""Exercise shared TTS cache with many identical synthesize requests."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


_HTTP_TIMEOUT_SECONDS = 15.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load test shared TTS cache by issuing concurrent synthesize requests.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Application base URL.")
    parser.add_argument("--lesson-id", default="", help="Existing lesson id. When omitted, a mock lesson is created.")
    parser.add_argument("--requests", type=int, default=100, help="Total TTS synthesize requests to send.")
    parser.add_argument("--concurrency", type=int, default=50, help="Maximum concurrent synthesize requests.")
    parser.add_argument("--return-mode", choices=["audio", "url"], default="url", help="TTS return_mode value.")
    parser.add_argument("--same-caption", action="store_true", help="Use the same caption_id for every request.")
    parser.add_argument("--provider", default="mock", help="TTS provider override. Defaults to mock.")
    parser.add_argument("--language", default="kk", help="TTS language.")
    parser.add_argument("--voice", default="mock-kk-1", help="TTS voice id.")
    parser.add_argument("--text", default="Same caption", help="Caption text to synthesize.")
    parser.add_argument("--caption-id", default="load-caption-1", help="Caption id used with --same-caption.")
    parser.add_argument("--token", default="", help="Optional lesson-scoped token for auth-enabled environments.")
    parser.add_argument("--student-token", default="", help="Optional signed lesson-scoped student token with tts:play.")
    parser.add_argument("--integration-key", default="", help="Optional backend integration key used only to create v1 lessons/tokens.")
    parser.add_argument("--use-v1", action="store_true", help="Use /api/v1/integration endpoints and a student tts:play token.")
    parser.add_argument("--timeout-seconds", type=float, default=15, help="Per-request HTTP timeout.")
    parser.add_argument("--disable-rate-limit-for-load-test", action="store_true", help="Send the dev-only TTS load-test rate-limit bypass header.")
    parser.add_argument("--allow-real-provider", action="store_true", help="Permit non-mock provider load tests.")
    return parser


def run(args: argparse.Namespace) -> int:
    global _HTTP_TIMEOUT_SECONDS
    _HTTP_TIMEOUT_SECONDS = max(0.1, float(args.timeout_seconds))
    if args.provider != "mock" and not args.allow_real_provider:
        print("Real-provider TTS load tests are intentionally blocked by default. Use --provider mock or pass --allow-real-provider deliberately.")
        return 2
    if args.use_v1 and args.return_mode == "url" and not _effective_student_token(args) and not args.integration_key:
        print("return_mode=url requires student tts:play token or integration token flow. Pass --student-token or --integration-key with --use-v1.")
        return 2
    if args.use_v1 and not args.lesson_id and not args.integration_key:
        print("v1 load tests need --lesson-id with --student-token, or --integration-key so the script can create a lesson.")
        return 2

    lesson_id = args.lesson_id or _create_mock_lesson(args)["lesson_id"]
    student_token = _effective_student_token(args)
    if args.use_v1 and not student_token and args.integration_key:
        student_token = _create_student_token(args, lesson_id)
        setattr(args, "student_token", student_token)
    before = _get_json(args.base_url, "/api/metrics/runtime")
    total_requests = max(0, int(args.requests))
    max_workers = max(1, min(int(args.concurrency), total_requests or 1))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_send_synthesize, args, lesson_id, index)
            for index in range(total_requests)
        ]
        results = []
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if args.return_mode == "url" and _synthesize_ok(result):
                audio_url = ((result.get("json") or {}).get("audio_url") or "")
                audio_result = _get_audio_url(args.base_url, audio_url) if audio_url else {"status": 0, "error": "Missing audio_url", "latency_ms": None}
                result["audio_url_status"] = audio_result.get("status")
                result["audio_url_latency_ms"] = audio_result.get("latency_ms")
                if not _http_ok(audio_result):
                    result["audio_url_error"] = _safe_error_sample(audio_result)
            results.append(result)

    after = _get_json(args.base_url, "/api/metrics/runtime")
    successes = [result for result in results if _result_ok(result)]
    failures = [result for result in results if result not in successes]
    latencies = [float(result["latency_ms"]) for result in successes if result.get("latency_ms") is not None]
    audio_url_success = sum(1 for result in results if args.return_mode == "url" and _http_ok({"status": result.get("audio_url_status", 0)}))
    audio_url_failed = sum(1 for result in results if args.return_mode == "url" and result.get("audio_url_status") is not None and not _http_ok({"status": result.get("audio_url_status", 0)}))
    auth_401_count = sum(1 for result in results if int(result.get("status", 0) or 0) == 401 or int(result.get("audio_url_status", 0) or 0) == 401)
    report = {
        "lesson_id": lesson_id,
        "provider": args.provider,
        "language": args.language,
        "voice": args.voice,
        "return_mode": args.return_mode,
        "same_caption": bool(args.same_caption),
        "total_requests": total_requests,
        "success": len(successes),
        "failed": len(failures),
        "audio_url_success": audio_url_success,
        "audio_url_failed": audio_url_failed,
        "auth_401_count": auth_401_count,
        "cache_hits": _metric_delta(before, after, "tts_cache_hits_total"),
        "cache_misses": _metric_delta(before, after, "tts_cache_misses_total"),
        "provider_calls_before": int(before.get("tts_provider_calls_total", 0) or 0),
        "provider_calls_after": int(after.get("tts_provider_calls_total", 0) or 0),
        "provider_calls_saved": _metric_delta(before, after, "tts_provider_calls_saved_total"),
        "avg_latency_ms": round(statistics.mean(latencies), 2) if latencies else None,
        "p95_latency_ms": _percentile(latencies, 95),
    }
    if failures:
        report["failure_samples"] = _failure_samples(failures)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


def _create_mock_lesson(args: argparse.Namespace) -> dict:
    if args.use_v1:
        return _post_json(
            args.base_url,
            "/api/v1/integration/lessons",
            {
                "external_lesson_id": f"tts-load-{int(time.time())}",
                "title": "TTS Cache Load Test",
                "mode": "mock",
                "stt_provider": "mock",
                "translation_provider": "mock",
                "create_zoom_meeting": False,
            },
            headers=_integration_headers(args),
        )
    return _post_json(
        args.base_url,
        "/api/lessons",
        {
            "title": "TTS Cache Load Test",
            "mode": "mock",
            "stt_provider": "mock",
            "translation_provider": "mock",
        },
    )


def _create_student_token(args: argparse.Namespace, lesson_id: str) -> str:
    payload = _post_json(
        args.base_url,
        f"/api/v1/integration/lessons/{lesson_id}/student-token",
        {
            "external_student_id": "tts-load-student",
            "display_name": "TTS Load Student",
            "scopes": ["tts:play"],
        },
        headers=_integration_headers(args),
    )
    return str(payload.get("token") or "")


def _send_synthesize(args: argparse.Namespace, lesson_id: str, index: int) -> dict:
    path = _synthesize_path(args, lesson_id)
    caption_id = args.caption_id if args.same_caption else f"{args.caption_id}-{index + 1}"
    payload = {
        "text": args.text,
        "language": args.language,
        "provider": args.provider,
        "voice": args.voice or None,
        "caption_id": caption_id,
        "sequence": index + 1,
        "return_mode": args.return_mode,
    }
    headers = {}
    if args.disable_rate_limit_for_load_test:
        headers["X-TTS-Load-Test-Bypass-Rate-Limit"] = "true"
    return _post_json(args.base_url, path, payload, headers=headers)


def _synthesize_path(args: argparse.Namespace, lesson_id: str) -> str:
    path = f"/api/v1/integration/lessons/{lesson_id}/tts/synthesize" if args.use_v1 else f"/api/lessons/{lesson_id}/tts/synthesize"
    token = _effective_student_token(args)
    if token:
        path = f"{path}?{urlencode({'token': token})}"
    return path


def _effective_student_token(args: argparse.Namespace) -> str:
    return str(getattr(args, "student_token", "") or getattr(args, "token", "") or "")


def _integration_headers(args: argparse.Namespace) -> dict:
    return {"X-Integration-Key": args.integration_key} if args.integration_key else {}


def _post_json(base_url: str, path: str, payload: dict, headers: dict | None = None) -> dict:
    started_at = time.perf_counter()
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = Request(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            body = response.read()
            latency_ms = (time.perf_counter() - started_at) * 1000
            content_type = response.headers.get("content-type", "")
            if "/tts/synthesize" not in path:
                return json.loads(body.decode("utf-8"))
            parsed_json = json.loads(body.decode("utf-8")) if "application/json" in content_type else None
            return {
                "status": response.status,
                "headers": {key.lower(): value for key, value in response.headers.items()},
                "json": parsed_json,
                "latency_ms": round(latency_ms, 2),
            }
    except HTTPError as exc:
        latency_ms = (time.perf_counter() - started_at) * 1000
        body = exc.read().decode("utf-8", errors="replace")
        if "/tts/synthesize" not in path:
            raise SystemExit(f"HTTP {exc.code}: {body}") from exc
        return {
            "status": exc.code,
            "headers": {key.lower(): value for key, value in exc.headers.items()},
            "error": body,
            "latency_ms": round(latency_ms, 2),
        }
    except URLError as exc:
        if "/tts/synthesize" not in path:
            raise SystemExit(f"Could not reach app: {exc}") from exc
        return {"status": 0, "error": f"Could not reach app: {exc}", "latency_ms": None}


def _get_audio_url(base_url: str, audio_url: str) -> dict:
    started_at = time.perf_counter()
    request = Request(_audio_url_request_url(base_url, audio_url))
    try:
        with urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            response.read()
            latency_ms = (time.perf_counter() - started_at) * 1000
            return {
                "status": response.status,
                "headers": {key.lower(): value for key, value in response.headers.items()},
                "latency_ms": round(latency_ms, 2),
            }
    except HTTPError as exc:
        latency_ms = (time.perf_counter() - started_at) * 1000
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "status": exc.code,
            "headers": {key.lower(): value for key, value in exc.headers.items()},
            "error": body,
            "latency_ms": round(latency_ms, 2),
        }
    except URLError as exc:
        return {"status": 0, "error": f"Could not reach app: {exc}", "latency_ms": None}


def _audio_url_request_url(base_url: str, audio_url: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", audio_url)


def _get_json(base_url: str, path: str) -> dict:
    request = Request(urljoin(base_url.rstrip("/") + "/", path.lstrip("/")))
    try:
        with urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise SystemExit(f"Could not reach app: {exc}") from exc


def _metric_delta(before: dict, after: dict, key: str) -> int:
    return int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0)


def _http_ok(result: dict) -> bool:
    return 200 <= int(result.get("status", 0) or 0) < 300


def _synthesize_ok(result: dict) -> bool:
    return _http_ok(result)


def _result_ok(result: dict) -> bool:
    if not _synthesize_ok(result):
        return False
    if result.get("audio_url_status") is not None:
        return 200 <= int(result.get("audio_url_status", 0) or 0) < 300
    return True


def _safe_error_sample(result: dict) -> str:
    error = str(result.get("error") or "")
    return error[:500]


def _failure_samples(failures: list[dict]) -> list[dict]:
    samples = []
    for failure in failures[:5]:
        sample = {
            "status": failure.get("status"),
            "audio_url_status": failure.get("audio_url_status"),
            "latency_ms": failure.get("latency_ms"),
            "audio_url_latency_ms": failure.get("audio_url_latency_ms"),
        }
        if failure.get("error"):
            sample["error"] = _safe_error_sample(failure)
        if failure.get("audio_url_error"):
            sample["audio_url_error"] = str(failure.get("audio_url_error"))[:500]
        samples.append(sample)
    return samples


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1)))
    return round(ordered[index], 2)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
