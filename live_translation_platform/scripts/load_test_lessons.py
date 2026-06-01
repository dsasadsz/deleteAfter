#!/usr/bin/env python
"""Create mock lesson load and report runtime metrics without real providers by default."""

from __future__ import annotations

import argparse
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare mock lessons and read runtime metrics for load testing.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Application base URL.")
    parser.add_argument("--lesson-id", action="append", default=[], help="Existing lesson id to use instead of creating a new lesson. Repeat for multiple lessons.")
    parser.add_argument("--lessons", type=int, default=1, help="Number of lessons to create.")
    parser.add_argument("--students", type=int, default=50, help="Expected simulated students per lesson for the report.")
    parser.add_argument("--duration-seconds", type=float, default=60, help="Observation window after lesson creation.")
    parser.add_argument("--mock", action="store_true", default=True, help="Use mock STT/translation providers.")
    parser.add_argument("--simulate-captions", action="store_true", help="Publish mock captions through the dev-only load-test endpoint.")
    parser.add_argument("--captions-per-second", type=float, default=1.0, help="Mock caption events per second per lesson when --simulate-captions is set.")
    parser.add_argument("--report-system", action="store_true", help="Include psutil CPU/RAM when psutil is installed.")
    return parser


def run(args: argparse.Namespace) -> int:
    if not args.mock:
        print("Real-provider load tests are intentionally manual. Rerun with --mock for the default safe mode.")
        return 2
    created = [{"lesson_id": lesson_id} for lesson_id in args.lesson_id]
    if not created:
        for index in range(max(0, args.lessons)):
            created.append(_create_mock_lesson(args.base_url, index))
    captions_published = 0
    if args.simulate_captions:
        captions_published = _publish_mock_captions(args, created, pace=True)
    else:
        time.sleep(max(0, args.duration_seconds))
    metrics = _get_json(args.base_url, "/api/metrics/runtime")
    report = {
        "mode": "mock",
        "lessons_requested": args.lessons,
        "lessons_created": len(created),
        "used_existing_lessons": bool(args.lesson_id),
        "students_per_lesson": args.students,
        "lesson_ids": [lesson.get("lesson_id") for lesson in created],
        "simulate_captions": bool(args.simulate_captions),
        "captions_per_second_per_lesson": args.captions_per_second if args.simulate_captions else 0,
        "captions_published": captions_published,
        "runtime_metrics": metrics,
    }
    if args.report_system:
        report["system"] = _system_report()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def _create_mock_lesson(base_url: str, index: int) -> dict:
    payload = {
        "title": f"Load Test Lesson {index + 1}",
        "mode": "mock",
        "stt_provider": "mock",
        "translation_provider": "mock",
    }
    return _post_json(base_url, "/api/lessons", payload)


def _publish_mock_captions(args: argparse.Namespace, lessons: list[dict], pace: bool = False) -> int:
    lesson_ids = [lesson.get("lesson_id") for lesson in lessons if lesson.get("lesson_id")]
    if not lesson_ids:
        return 0
    captions_per_second = max(0.0, float(args.captions_per_second))
    duration_seconds = max(0.0, float(args.duration_seconds))
    events_per_lesson = int(captions_per_second * duration_seconds)
    if captions_per_second <= 0 or events_per_lesson <= 0:
        return 0

    interval_seconds = 1.0 / captions_per_second
    started_at = time.monotonic()
    published = 0
    for sequence in range(1, events_per_lesson + 1):
        for lesson_id in lesson_ids:
            _post_json(
                args.base_url,
                f"/api/load-test/lessons/{lesson_id}/publish-caption",
                _mock_caption_payload(sequence),
            )
            published += 1
        if pace and sequence < events_per_lesson:
            target_at = started_at + (sequence * interval_seconds)
            time.sleep(max(0.0, target_at - time.monotonic()))
    return published


def _mock_caption_payload(sequence: int) -> dict:
    return {
        "sequence": sequence,
        "original_text": f"Mock load-test caption {sequence}",
        "translations": {
            "kk": f"Mock load-test caption {sequence} kk",
            "uz": f"Mock load-test caption {sequence} uz",
        },
        "latency_ms": {"stt": 0, "translation": 5, "total": 10},
    }


def _post_json(base_url: str, path: str, payload: dict) -> dict:
    request = Request(
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _request_json(request)


def _get_json(base_url: str, path: str) -> dict:
    return _request_json(Request(urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))))


def _request_json(request: Request) -> dict:
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc
    except URLError as exc:
        raise SystemExit(f"Could not reach app: {exc}") from exc


def _system_report() -> dict:
    try:
        import psutil
    except Exception:
        return {"available": False}
    return {
        "available": True,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory_percent": psutil.virtual_memory().percent,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
