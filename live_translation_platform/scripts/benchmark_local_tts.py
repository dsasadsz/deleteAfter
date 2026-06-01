from __future__ import annotations

import argparse
import asyncio
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings
from app.tts.cache import TTSCache, synthesize_with_cache
from app.tts.factory import create_tts_provider
from app.tts.local_engines.base import LocalTTSEngine, LocalTTSSynthesisResult, local_voice
from app.tts.local_tts import local_tts_provider_kwargs


PHRASES = [
    "Сәлем, сынып.",
    "Бүгін біз айнымалыларды үйренеміз.",
    "Код редакторын ашыңыз.",
    "Мына жолдағы қатені табыңыз.",
    "Функция мән қайтарады.",
    "Цикл әр элемент бойынша жүреді.",
    "Сұрақтарыңызды чатқа жазыңыз.",
    "Үй тапсырмасы массивтер туралы болады.",
    "Привет, класс.",
    "Откройте редактор кода.",
    "Запустите программу.",
    "Проверьте результат.",
    "Bu funksiyani tekshiring.",
    "Massiv bo'sh emasligini tekshiring.",
    "请打开代码编辑器。",
    "请检查程序结果。",
]


async def main() -> int:
    args = _parse_args()
    provider = _provider(args.engine, fake_backend=args.fake_backend)
    phrases = PHRASES[: max(1, min(args.text_count, len(PHRASES)))]
    records = []
    failures = 0
    timeouts = 0
    total_bytes = 0
    selected_voice = args.voice or None
    cold_start_ms = None

    for index, phrase in enumerate(phrases, start=1):
        started_at = perf_counter()
        try:
            result = await provider.synthesize(phrase, args.language, args.voice or None)
            latency_ms = (perf_counter() - started_at) * 1000
            if cold_start_ms is None:
                cold_start_ms = latency_ms
            total_bytes += len(result.audio_bytes)
            selected_voice = result.voice or selected_voice
            records.append(
                {
                    "index": index,
                    "text_preview": _truncate(phrase),
                    "language": args.language,
                    "voice": result.voice,
                    "latency_ms": latency_ms,
                    "bytes": len(result.audio_bytes),
                    "content_type": result.content_type,
                    "ok": True,
                    "error": None,
                }
            )
            print(f"{index:02d} {latency_ms:8.2f} ms {len(result.audio_bytes):8d} bytes ok")
        except Exception as exc:
            failures += 1
            if "timeout" in str(exc).lower() or "timeout" in exc.__class__.__name__.lower():
                timeouts += 1
            latency_ms = (perf_counter() - started_at) * 1000
            records.append(
                {
                    "index": index,
                    "text_preview": _truncate(phrase),
                    "language": args.language,
                    "voice": args.voice or None,
                    "latency_ms": latency_ms,
                    "bytes": 0,
                    "content_type": None,
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {_truncate(str(exc), 180)}",
                }
            )
            print(f"{index:02d} {latency_ms:8.2f} ms fail {exc.__class__.__name__}: {_truncate(str(exc), 120)}")

    status = provider.status() if hasattr(provider, "status") else {}
    engine_status = status.get("engines", {}).get(args.engine, {}) if isinstance(status, dict) else {}
    cache_report = await _cache_smoke(provider, args) if args.test_cache else {"tested": False, "supported": True}
    summary = build_summary(
        engine=args.engine,
        language=args.language,
        voice=selected_voice,
        records=records,
        failures=failures,
        timeouts=timeouts or int(engine_status.get("timeout_count") or 0),
        total_bytes=total_bytes,
        engine_status=engine_status,
        cache_report=cache_report,
        fake_backend=bool(args.fake_backend),
        cold_start_ms=cold_start_ms,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    output = _report_path(args)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"summary": summary, "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {output}")
    return 1 if failures else 0


def build_summary(
    *,
    engine: str,
    language: str,
    voice: str | None,
    records: list[dict],
    failures: int,
    timeouts: int,
    total_bytes: int,
    engine_status: dict,
    cache_report: dict,
    fake_backend: bool,
    cold_start_ms: float | None,
) -> dict:
    latencies = [item["latency_ms"] for item in records if item.get("ok")]
    return {
        "provider": "local",
        "engine": _engine_key(engine),
        "language": language,
        "voice": voice,
        "output_format": engine_status.get("output_format"),
        "device": engine_status.get("device"),
        "engine_status": engine_status.get("status"),
        "configured_timeout_seconds": engine_status.get("timeout_seconds"),
        "cold_start_ms": cold_start_ms,
        "count": len(records),
        "successes": len(latencies),
        "failures": failures,
        "timeouts": timeouts,
        "average_ms": mean(latencies) if latencies else None,
        "p50_ms": median(latencies) if latencies else None,
        "p95_ms": _percentile(latencies, 95) if latencies else None,
        "total_audio_bytes": total_bytes,
        "average_audio_bytes": total_bytes / len(latencies) if latencies else None,
        "sample_text_preview": _sample_text_preview(records),
        "cache": cache_report,
        "fake_backend": fake_backend,
        "environment": environment_summary(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def environment_summary() -> dict:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": _cpu_count(),
        "ram_total_bytes": _ram_total_bytes(),
        "gpu": _gpu_summary(),
    }


def _provider(engine: str, *, fake_backend: bool = False):
    normalized = _engine_key(engine)
    if fake_backend:
        return create_tts_provider(
            "local",
            enabled=True,
            default_engine=normalized,
            ru_engine=normalized if normalized == "silero" else "silero",
            kk_engine=normalized if normalized == "kazakh_tts2" else "piper",
            uz_engine=normalized if normalized == "piper" else "piper",
            zh_engine=normalized if normalized == "piper" else "piper",
            engines={normalized: FakeBenchmarkEngine(normalized)},
        )
    settings = get_settings()
    kwargs = local_tts_provider_kwargs(settings)
    kwargs["default_engine"] = normalized
    if normalized == "silero":
        kwargs["ru_engine"] = "silero"
    if normalized == "piper":
        kwargs["kk_engine"] = "piper"
        kwargs["uz_engine"] = "piper"
        kwargs["zh_engine"] = "piper"
    if normalized == "kazakh_tts2":
        kwargs["kk_engine"] = "kazakh_tts2"
    return create_tts_provider("local", **kwargs)


async def _cache_smoke(provider, args) -> dict:
    cache = TTSCache(max_items=4)
    phrase = PHRASES[0]
    try:
        miss_started = perf_counter()
        first = await synthesize_with_cache(cache, provider, phrase, args.language, args.voice or None, "wav")
        miss_latency_ms = (perf_counter() - miss_started) * 1000
        hit_started = perf_counter()
        second = await synthesize_with_cache(cache, provider, phrase, args.language, args.voice or None, "wav")
        hit_latency_ms = (perf_counter() - hit_started) * 1000
        return {
            "tested": True,
            "supported": True,
            "first_cached": first.cached,
            "second_cached": second.cached,
            "same_bytes": first.audio_bytes == second.audio_bytes,
            "miss_latency_ms": miss_latency_ms,
            "hit_latency_ms": hit_latency_ms,
            "cache_miss_p95_ms": miss_latency_ms,
            "cache_hit_p95_ms": hit_latency_ms,
        }
    except Exception as exc:
        return {"tested": True, "supported": True, "error": f"{exc.__class__.__name__}: {_truncate(str(exc), 180)}"}


class FakeBenchmarkEngine(LocalTTSEngine):
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def supports(self, language: str) -> bool:
        return True

    def default_voice_for_language(self, language: str) -> str:
        return f"{self.name}-{language.replace('zh-Hans', 'zh')}"

    def voice_catalog(self) -> dict[str, list[dict]]:
        return {
            language: [local_voice(language, self.name, self.default_voice_for_language(language))]
            for language in ("kk", "uz", "zh-Hans", "ru")
        }

    def status(self) -> dict:
        return {
            "ready": True,
            "status": "ready",
            "enabled": True,
            "missing": [],
            "engine": self.name,
            "device": "fake",
            "timeout_seconds": 0,
            "timeout_count": 0,
            "content_type": "audio/wav",
            "output_format": "wav",
        }

    def status_for_language(self, language: str) -> dict:
        return self.status()

    async def synthesize(self, text: str, language: str, voice: str | None = None, audio_format: str | None = None) -> LocalTTSSynthesisResult:
        self.calls += 1
        return LocalTTSSynthesisResult(b"RIFFfake-benchmark", "audio/wav", 100)


def _engine_key(engine: str) -> str:
    normalized = (engine or "piper").strip().lower().replace("-", "_")
    if normalized in {"piper", "silero", "kazakh_tts2"}:
        return normalized
    raise SystemExit(f"Unsupported local TTS engine: {engine}")


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((percentile / 100) * (len(ordered) - 1))
    return ordered[int(index)]


def _report_path(args) -> Path | None:
    if args.json_report:
        return Path(args.json_report)
    if args.write_report:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return Path("reports") / f"local_tts_benchmark_{stamp}.json"
    return None


def _sample_text_preview(records: list[dict]) -> str | None:
    for item in records:
        if item.get("ok"):
            return _truncate(item.get("text_preview", ""))
    return None


def _truncate(value: object, limit: int = 120) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _cpu_count() -> int | None:
    try:
        import os

        return os.cpu_count()
    except Exception:
        return None


def _ram_total_bytes() -> int | None:
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except Exception:
        return None


def _gpu_summary() -> str | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        output = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except Exception:
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return "; ".join(lines) if lines else None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark local Piper/Silero/KazakhTTS2 TTS latency.")
    parser.add_argument("--engine", default="piper", choices=["piper", "silero", "kazakh_tts2"])
    parser.add_argument("--language", default="kk")
    parser.add_argument("--voice", default="")
    parser.add_argument("--text-count", type=int, default=16)
    parser.add_argument("--test-cache", action="store_true", help="Run a tiny provider-cache smoke check.")
    parser.add_argument("--fake-backend", action="store_true", help="Use an in-process fake local engine for CI/smoke only.")
    parser.add_argument("--write-report", action="store_true", help="Write reports/local_tts_benchmark_*.json.")
    parser.add_argument("--json-report", default="", help="Write JSON report to a specific path.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
