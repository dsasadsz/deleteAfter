from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings
from app.stt.base import create_stt_provider
from app.stt.faster_whisper_stt import faster_whisper_provider_kwargs


async def main() -> int:
    args = _parse_args()
    wav_path = Path(args.wav_file)
    if not wav_path.exists():
        print(f"WAV file not found: {wav_path}", file=sys.stderr)
        return 2

    settings = get_settings()
    provider = create_stt_provider("faster_whisper", **faster_whisper_provider_kwargs(settings))
    report = {
        "provider": "faster_whisper",
        "wav_file": str(wav_path),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        started_at = perf_counter()
        await provider.connect()
        await _send_wav(provider, wav_path, chunk_ms=args.chunk_ms)
        await provider.commit("smoke_wav_complete")
        event = await _next_final_event(provider, timeout_seconds=args.timeout)
        latency_ms = (perf_counter() - started_at) * 1000
        report.update(
            {
                "ok": True,
                "transcript": event.text,
                "language": event.language,
                "confidence": event.confidence,
                "latency_ms": latency_ms,
                "provider_status": provider.status(),
            }
        )
        print(f"language={event.language}")
        print(f"latency_ms={latency_ms:.2f}")
        print(f"transcript={event.text}")
    except Exception as exc:
        report.update({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
        print(report["error"], file=sys.stderr)
        return_code = 1
    else:
        return_code = 0
    finally:
        await provider.close()

    if args.write_report:
        output = _report_path(args, wav_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {output}")
    return return_code


async def _send_wav(provider, wav_path: Path, *, chunk_ms: int) -> None:
    with wave.open(str(wav_path), "rb") as wav_file:
        sample_width = wav_file.getsampwidth()
        if sample_width != 2:
            raise ValueError(f"Expected 16-bit PCM WAV, got sample width {sample_width}")
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        frames_per_chunk = max(1, int(sample_rate * max(1, chunk_ms) / 1000))
        while True:
            data = wav_file.readframes(frames_per_chunk)
            if not data:
                break
            await provider.send_audio(
                data,
                {
                    "sample_rate": sample_rate,
                    "channels": channels,
                    "format": "pcm_s16le",
                    "audio_received_at": datetime.utcnow(),
                    "source": "smoke_wav",
                    "speaker_id": "teacher",
                },
            )


async def _next_final_event(provider, *, timeout_seconds: float):
    async def wait():
        async for event in provider.events():
            if event.is_final:
                return event
        raise RuntimeError("Provider closed before final transcript")

    return await asyncio.wait_for(wait(), timeout=timeout_seconds)


def _report_path(args: argparse.Namespace, wav_path: Path) -> Path:
    if args.report_path:
        return Path(args.report_path)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Path("reports") / f"faster_whisper_stt_smoke_{wav_path.stem}_{timestamp}.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test the local faster-whisper STT provider with a WAV file.")
    parser.add_argument("wav_file", help="Path to a 16-bit PCM WAV file.")
    parser.add_argument("--chunk-ms", type=int, default=100, help="Chunk size to feed the provider.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Timeout waiting for the final transcript.")
    parser.add_argument("--write-report", action="store_true", help="Write a JSON smoke report.")
    parser.add_argument("--report-path", default="", help="Optional explicit report path.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
