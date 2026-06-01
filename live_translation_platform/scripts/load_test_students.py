#!/usr/bin/env python
"""Simulate student caption WebSocket clients without invoking providers."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse, urlunparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open simulated student caption WebSocket clients and collect receive metrics.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Application base URL.")
    parser.add_argument("--lesson-id", required=False, default="lesson_load_test", help="Lesson id to subscribe to.")
    parser.add_argument("--students", type=int, default=50, help="Number of simulated caption WebSocket clients.")
    parser.add_argument("--duration-seconds", type=float, default=60, help="How long clients should listen.")
    parser.add_argument("--token", default="", help="Optional lesson access token.")
    parser.add_argument("--connect-timeout-seconds", type=float, default=10, help="Per-client WebSocket connect timeout.")
    parser.add_argument("--no-tts", action="store_true", default=True, help="Do not request TTS during this load test.")
    return parser


async def run(args: argparse.Namespace) -> int:
    try:
        import websockets
    except ImportError:
        print("The optional 'websockets' package is required to run the student load test.")
        print("Install it only for load-test environments, then rerun this script.")
        return 2

    url = _websocket_url(args.base_url, f"/ws/lessons/{args.lesson_id}/captions", args.token)
    stop_at = time.monotonic() + args.duration_seconds
    results = await asyncio.gather(
        *[_student_client(index, websockets, url, stop_at, args.connect_timeout_seconds) for index in range(max(0, args.students))],
    )
    counts = [result["received_count"] for result in results]
    latencies = [latency for result in results for latency in result["latencies_ms"]]
    failures = [result for result in results if result["errors"]]
    report = {
        "students": args.students,
        "connected": sum(1 for result in results if result["connected"]),
        "failures": len(failures),
        "disconnects": sum(result["disconnects"] for result in results),
        "captions_received_total": sum(counts),
        "captions_received_min": min(counts) if counts else 0,
        "captions_received_max": max(counts) if counts else 0,
        "latency_ms_avg": round(statistics.mean(latencies), 2) if latencies else None,
        "latency_ms_p95": _percentile(latencies, 95),
        "per_client": [_client_summary(result) for result in results],
        "tts_enabled": False,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


async def _student_client(index: int, websockets, url: str, stop_at: float, timeout: float) -> dict:
    result = {
        "client_index": index,
        "connected": False,
        "received_count": 0,
        "latencies_ms": [],
        "disconnects": 0,
        "errors": [],
    }
    try:
        async with websockets.connect(url, open_timeout=timeout) as websocket:
            result["connected"] = True
            while time.monotonic() < stop_at:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=max(0.1, stop_at - time.monotonic()))
                except asyncio.TimeoutError:
                    break
                result["received_count"] += 1
                latency = _message_latency_ms(message)
                if latency is not None:
                    result["latencies_ms"].append(latency)
    except Exception as exc:
        result["disconnects"] += 1 if result["connected"] else 0
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    return result


def _websocket_url(base_url: str, path: str, token: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urlencode({"token": token}) if token else ""
    return urlunparse((scheme, parsed.netloc, path, "", query, ""))


def _message_latency_ms(message: str | bytes) -> float | None:
    try:
        payload = json.loads(message)
    except Exception:
        return None
    published_at = payload.get("load_test_published_at")
    if published_at:
        latency = _iso_timestamp_latency_ms(str(published_at))
        if latency is not None:
            return latency
    latency = payload.get("latency_ms") or {}
    total = latency.get("estimated_end_to_end_latency_ms") or latency.get("total") or latency.get("total_latency_ms")
    return float(total) if total is not None else None


def _iso_timestamp_latency_ms(value: str) -> float | None:
    try:
        normalized = value.replace("Z", "+00:00")
        published_at = datetime.fromisoformat(normalized)
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - published_at.astimezone(timezone.utc)).total_seconds() * 1000)


def _client_summary(result: dict) -> dict:
    latencies = result["latencies_ms"]
    return {
        "client_index": result["client_index"],
        "connected": result["connected"],
        "received_count": result["received_count"],
        "disconnects": result["disconnects"],
        "errors": result["errors"],
        "latency_ms_avg": round(statistics.mean(latencies), 2) if latencies else None,
        "latency_ms_p95": _percentile(latencies, 95),
    }


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1)))
    return round(ordered[index], 2)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
