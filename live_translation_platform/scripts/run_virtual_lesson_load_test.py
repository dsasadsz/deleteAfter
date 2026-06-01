#!/usr/bin/env python
"""Start a local virtual lesson bot load test through the web API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urljoin


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.loadtest.report_builder import sanitize_for_report, write_json_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run async virtual teacher/student lesson bots through the local load-test API.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="HTTP application base URL.")
    parser.add_argument("--ws-base-url", default="ws://127.0.0.1:8000", help="WebSocket application base URL for bot clients.")
    parser.add_argument("--sessions", type=int, default=3, choices=[1, 3, 6], help="Concurrent lesson sessions.")
    parser.add_argument("--students-per-session", type=int, default=90, help="Virtual students per lesson session.")
    parser.add_argument("--mode", choices=["light", "real_pipeline", "full"], default="light", help="Provider mode.")
    parser.add_argument("--audio-wav", default="", help="Uploaded WAV/MP3 path to send to the API before starting.")
    parser.add_argument("--reference-text", default="", help="Reference transcript text file path.")
    parser.add_argument("--reference-text-inline", default="", help="Reference transcript text literal.")
    parser.add_argument("--target-languages", default="kk,uz,zh-Hans", help="Comma-separated target languages.")
    parser.add_argument("--tts-languages", default="kk,zh-Hans", help="Comma-separated TTS languages.")
    parser.add_argument("--tts-request-ratio", type=float, default=0.25, help="Fraction of students that request TTS for final captions.")
    parser.add_argument("--duration-limit-seconds", type=int, default=240, help="Maximum run duration.")
    parser.add_argument("--audio-chunk-ms", type=int, default=50, help="Teacher audio chunk size in milliseconds.")
    parser.add_argument("--force-commit-every-seconds", type=int, default=3, help="Teacher commit marker interval.")
    parser.add_argument("--integration-key", default="", help="Integration/admin key for starting and stopping tests. Never printed unredacted.")
    parser.add_argument("--no-wait", action="store_true", help="Return immediately after the API accepts the run.")
    parser.add_argument("--write-report", action="store_true", help="Download and write JSON/Markdown/HTML reports after start.")
    parser.add_argument("--report-dir", default="reports/local_load_tests", help="Directory for downloaded reports.")
    return parser


async def run_async(args: argparse.Namespace) -> dict:
    import httpx

    headers = {"x-integration-key": args.integration_key} if args.integration_key else {}
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=30.0) as client:
        audio_file_id = await _upload_audio_if_needed(client, args.audio_wav, headers)
        payload = {
            "sessions": args.sessions,
            "students_per_session": args.students_per_session,
            "mode": args.mode,
            "audio_file_id": audio_file_id,
            "reference_ru_text": _reference_text(args),
            "target_languages": _csv(args.target_languages),
            "tts_languages": _csv(args.tts_languages),
            "tts_request_ratio": args.tts_request_ratio,
            "duration_limit_seconds": args.duration_limit_seconds,
            "audio_chunk_ms": args.audio_chunk_ms,
            "force_commit_every_seconds": args.force_commit_every_seconds,
        }
        response = await client.post("/api/load-tests/local", json=payload, headers=headers)
        response.raise_for_status()
        run = response.json()
        if not args.no_wait:
            run = await _wait_for_run(client, run["run_id"], timeout_seconds=max(15, args.duration_limit_seconds + 15))
        if args.write_report:
            await _write_reports(client, run["run_id"], Path(args.report_dir))
        return sanitize_for_report(run)


async def _upload_audio_if_needed(client, audio_path: str, headers: dict[str, str]) -> str | None:
    if not audio_path:
        return None
    path = Path(audio_path)
    with path.open("rb") as handle:
        response = await client.post("/api/load-tests/local/audio", files={"file": (path.name, handle, _content_type(path))}, headers=headers)
    response.raise_for_status()
    return response.json()["audio_file_id"]


async def _write_reports(client, run_id: str, report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_response = await client.get(f"/api/load-tests/local/{run_id}/report/json")
    json_response.raise_for_status()
    report = sanitize_for_report(json_response.json())
    write_json_report(report, report_dir / f"{run_id}.json")
    for suffix, path in (("markdown", report_dir / f"{run_id}.md"), ("html", report_dir / f"{run_id}.html")):
        response = await client.get(f"/api/load-tests/local/{run_id}/report/{suffix}")
        response.raise_for_status()
        path.write_text(response.text, encoding="utf-8")


async def _wait_for_run(client, run_id: str, *, timeout_seconds: int) -> dict:
    terminal = {"completed", "failed", "cancelled", "stopped"}
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    latest = {}
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/api/load-tests/local/{run_id}")
        response.raise_for_status()
        latest = response.json()
        if latest.get("status") in terminal:
            return latest
        await asyncio.sleep(1.0)
    return latest


def _reference_text(args: argparse.Namespace) -> str:
    if args.reference_text_inline:
        return args.reference_text_inline
    if args.reference_text:
        return Path(args.reference_text).read_text(encoding="utf-8")
    return ""


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _content_type(path: Path) -> str:
    if path.suffix.lower() == ".mp3":
        return "audio/mpeg"
    return "audio/wav"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(run_async(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps(sanitize_for_report({"error": str(exc)}), indent=2, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
