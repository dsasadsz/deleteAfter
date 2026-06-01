from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.audio.mock_audio_source import MockAudioSource
from app.config import get_settings
from app.realtime.audio_pipeline import AudioPipeline
from app.stt.base import STTEvent, create_stt_provider
from app.stt.faster_whisper_stt import faster_whisper_provider_kwargs
from app.translation.base import create_translation_provider
from app.translation.local_provider import local_translation_provider_kwargs
from app.tts.cache import TTSCache, synthesize_with_cache
from app.tts.factory import create_tts_provider
from app.tts.local_tts import local_tts_provider_kwargs


SECRET_KEY_RE = re.compile(r"(authorization|token|api[_-]?key|apikey|password|secret|credential)", re.IGNORECASE)
HF_TOKEN_RE = re.compile(r"hf_[A-Za-z0-9_]+")
SECRET_QUERY_RE = re.compile(r"([?&](?:token|access_token|api_key|apikey|key|password|secret)=)[^&#\s]+", re.IGNORECASE)


async def main() -> int:
    args = parse_args()
    started_at = perf_counter()
    errors: list[str] = []
    timings: dict[str, float] = {}
    stt_status: dict = {}
    translation_status: dict = {}
    fallback_used = False
    tts_cache_test = None
    tts_result = None
    transcript = ""
    translations: dict[str, str] = {}

    try:
        stt_started = perf_counter()
        stt_provider = create_stt_provider(args.stt_provider, **stt_kwargs(args.stt_provider))
        transcript, stt_status = await transcribe_wav(stt_provider, Path(args.audio_wav), chunk_ms=args.chunk_ms, timeout_seconds=args.stt_timeout)
        timings["stt_ms"] = (perf_counter() - stt_started) * 1000

        translation_started = perf_counter()
        translator = translation_provider(args.translation_provider, args.translation_fallback)
        before_fallback_count = provider_fallback_count(translator)
        caption_payloads: list[dict] = []
        pipeline = AudioPipeline(
            lesson_id="local_pipeline_e2e",
            meeting_id="local-smoke",
            source=MockAudioSource(interval_seconds=0.01, max_chunks=0),
            stt=stt_provider,
            translator=translator,
            target_languages=target_languages(args.target_languages),
            translate_partials=False,
            publish=lambda payload: capture_async(caption_payloads, payload),
            save_caption=lambda payload: None,
            save_metric=lambda payload: None,
            publish_debug=lambda payload: None,
            source_language="ru-RU",
        )
        await pipeline._handle_event(
            STTEvent(
                text=transcript,
                is_partial=False,
                is_final=True,
                language="ru-RU",
                confidence=None,
                provider=args.stt_provider,
                timestamp=datetime.utcnow(),
                raw={"audio_source": "smoke_local_pipeline_e2e", "audio": {"wav_file": args.audio_wav}},
            )
        )
        caption = next((item for item in caption_payloads if item.get("is_final")), None)
        if caption is None:
            raise RuntimeError("AudioPipeline did not publish a final caption")
        translations = dict(caption.get("translations") or {})
        translation_status = provider_status(translator)
        fallback_used = provider_fallback_count(translator) > before_fallback_count or translations_look_mock(translations)
        timings["translation_ms"] = (perf_counter() - translation_started) * 1000

        tts_started = perf_counter()
        tts = tts_provider(args.tts_engine)
        tts_text = translations.get(args.tts_language) or next(iter(translations.values()), transcript)
        cache = TTSCache(max_items=4)
        first = await synthesize_with_cache(cache, tts, tts_text, args.tts_language, None, "wav", {"caption_id": caption.get("caption_id")})
        second = await synthesize_with_cache(cache, tts, tts_text, args.tts_language, None, "wav", {"caption_id": caption.get("caption_id")})
        tts_result = first
        tts_cache_test = {
            "tested": True,
            "first_cached": first.cached,
            "second_cached": second.cached,
            "same_bytes": first.audio_bytes == second.audio_bytes,
        }
        timings["tts_ms"] = (perf_counter() - tts_started) * 1000
    except Exception as exc:
        errors.append(f"{exc.__class__.__name__}: {exc}")

    timings["total_ms"] = (perf_counter() - started_at) * 1000
    report = aggregate_report(
        options=args,
        timings=timings,
        transcript=transcript,
        stt_status=stt_status,
        translations=translations,
        translation_status=translation_status,
        fallback_used=fallback_used,
        tts_result=tts_result,
        tts_cache_test=tts_cache_test,
        errors=errors,
    )
    report = sanitize_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.write_report or args.report:
        write_reports(report, args)
    return 0 if report["verdict"] in {"PASS", "DEGRADED"} else 1


async def transcribe_wav(provider, wav_path: Path, *, chunk_ms: int, timeout_seconds: float) -> tuple[str, dict]:
    if not wav_path.exists():
        raise FileNotFoundError(str(wav_path))
    if hasattr(provider, "timeout_seconds"):
        provider.timeout_seconds = max(float(getattr(provider, "timeout_seconds", 0) or 0), float(timeout_seconds))
    if hasattr(provider, "segment_seconds"):
        provider.segment_seconds = max(float(getattr(provider, "segment_seconds", 0) or 0), 3600.0)
    await provider.connect()
    with wave.open(str(wav_path), "rb") as wav_file:
        if wav_file.getsampwidth() != 2:
            raise ValueError("Expected 16-bit PCM WAV")
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        frames_per_chunk = max(1, int(sample_rate * chunk_ms / 1000))
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
    await provider.commit("smoke_wav_complete")
    event = await next_final_event(provider, timeout_seconds=timeout_seconds)
    status = provider.status() if hasattr(provider, "status") else {}
    await provider.close()
    return event.text, status


async def next_final_event(provider, *, timeout_seconds: float):
    async def wait():
        async for event in provider.events():
            if event.is_final:
                return event
        raise RuntimeError("STT provider closed before final event")

    return await asyncio.wait_for(wait(), timeout=timeout_seconds)


def stt_kwargs(provider_name: str) -> dict:
    settings = get_settings()
    if provider_name == "faster_whisper":
        return faster_whisper_provider_kwargs(settings)
    if provider_name == "mock":
        return {"source_language": settings.source_language}
    return {}


def translation_provider(provider_name: str, fallback_engine: str):
    settings = get_settings()
    if provider_name == "local":
        kwargs = local_translation_provider_kwargs(settings)
        kwargs["enabled"] = True
        kwargs["fallback_engine"] = fallback_engine
        return create_translation_provider("local", **kwargs)
    return create_translation_provider(provider_name)


def tts_provider(engine: str):
    settings = get_settings()
    kwargs = local_tts_provider_kwargs(settings)
    kwargs["enabled"] = True
    kwargs["default_engine"] = engine
    kwargs["kk_engine"] = engine if engine == "piper" else kwargs.get("kk_engine", "piper")
    kwargs["ru_engine"] = engine if engine == "piper" else kwargs.get("ru_engine", "piper")
    return create_tts_provider("local", **kwargs)


def aggregate_report(
    *,
    options,
    timings: dict[str, float],
    transcript: str,
    stt_status: dict,
    translations: dict[str, str],
    translation_status: dict,
    fallback_used: bool,
    tts_result,
    tts_cache_test: dict | None,
    errors: list[str],
) -> dict:
    translation_summary = local_translation_status_summary(translation_status)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "verdict": build_verdict(errors=errors, fallback_used=fallback_used),
        "input": {"audio_wav": getattr(options, "audio_wav", "")},
        "providers": {
            "stt": getattr(options, "stt_provider", ""),
            "translation": getattr(options, "translation_provider", ""),
            "translation_fallback": getattr(options, "translation_fallback", ""),
            "tts_engine": getattr(options, "tts_engine", ""),
        },
        "stt": {
            "transcript": transcript,
            "latency_ms": timings.get("stt_ms"),
            "status": stt_status,
        },
        "translation": {
            "target_languages": target_languages(getattr(options, "target_languages", "")),
            "outputs": translations,
            "latency_ms": timings.get("translation_ms"),
            "fallback_used": fallback_used,
            **translation_summary,
        },
        "tts": {
            "language": getattr(options, "tts_language", ""),
            "bytes": len(tts_result.audio_bytes) if tts_result is not None else 0,
            "latency_ms": timings.get("tts_ms"),
            "content_type": getattr(tts_result, "content_type", None),
            "voice": getattr(tts_result, "voice", None),
            "cache": tts_cache_test or {"tested": False},
        },
        "total_latency_ms": timings.get("total_ms"),
        "errors": errors,
    }


def local_translation_status_summary(status: dict) -> dict:
    engines = status.get("engines") if isinstance(status, dict) else {}
    tilmash = engines.get("tilmash", {}) if isinstance(engines, dict) else {}
    return {
        "provider_status": status.get("status") if isinstance(status, dict) else None,
        "tilmash_status": tilmash.get("status", "missing"),
        "tilmash_missing": tilmash.get("missing", []),
    }


def build_verdict(*, errors: list[str], fallback_used: bool) -> str:
    if errors:
        return "FAIL"
    if fallback_used:
        return "DEGRADED"
    return "PASS"


def provider_status(provider) -> dict:
    if not hasattr(provider, "status"):
        return {}
    try:
        return provider.status()
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def provider_fallback_count(provider) -> int:
    status = provider_status(provider)
    metrics = status.get("metrics") if isinstance(status, dict) else {}
    return int(metrics.get("fallback_count") or 0) if isinstance(metrics, dict) else 0


def translations_look_mock(translations: dict[str, str]) -> bool:
    return any(value.startswith(f"[{language} mock]") for language, value in translations.items())


def target_languages(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


async def capture_async(items: list, payload: dict) -> None:
    items.append(payload)


def sanitize_report(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, child in value.items():
            if SECRET_KEY_RE.search(str(key)):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = sanitize_report(child)
        return sanitized
    if isinstance(value, list):
        return [sanitize_report(item) for item in value]
    if isinstance(value, str):
        return SECRET_QUERY_RE.sub(r"\1[redacted]", HF_TOKEN_RE.sub("[redacted]", value))
    return value


def write_reports(report: dict, args) -> None:
    json_path = Path(args.report) if args.report else default_report_path()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = json_path.with_suffix(".md")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")


def markdown_report(report: dict) -> str:
    translation = report.get("translation", {})
    tts = report.get("tts", {})
    return "\n".join(
        [
            f"# Local Pipeline E2E Smoke: {report.get('verdict')}",
            "",
            f"- STT transcript: {report.get('stt', {}).get('transcript', '')}",
            f"- Translation fallback used: {translation.get('fallback_used')}",
            f"- Tilmash status: {translation.get('tilmash_status')}",
            f"- TTS language: {tts.get('language')}",
            f"- TTS bytes: {tts.get('bytes')}",
            f"- Total latency ms: {report.get('total_latency_ms')}",
            "",
        ]
    )


def default_report_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Path("reports") / f"local_pipeline_e2e_{timestamp}.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke test local WAV -> faster-whisper -> translation -> caption -> Piper TTS.")
    parser.add_argument("--audio-wav", required=True, help="Path to a 16-bit PCM WAV file.")
    parser.add_argument("--stt-provider", default="faster_whisper", choices=["faster_whisper", "mock"])
    parser.add_argument("--translation-provider", default="local", choices=["local", "mock"])
    parser.add_argument("--translation-fallback", default="mock", choices=["mock", "madlad400", ""])
    parser.add_argument("--target-languages", default="kk,uz")
    parser.add_argument("--tts-engine", default="piper", choices=["piper"])
    parser.add_argument("--tts-language", default="kk")
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument("--stt-timeout", type=float, default=30.0)
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--report", default="")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
