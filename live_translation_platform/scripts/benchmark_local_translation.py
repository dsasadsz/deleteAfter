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
from app.translation.base import TranslationProvider, create_translation_provider
from app.translation.local_provider import local_translation_provider_kwargs


PHRASES = [
    "Сегодня мы изучаем переменные в C#.",
    "Откройте редактор кода и создайте новый файл.",
    "Сейчас я объясню, как работает цикл for.",
    "Обратите внимание на тип данных string.",
    "Эта функция возвращает значение.",
    "Добавьте условие if перед вызовом метода.",
    "Проверьте, что массив не пустой.",
    "Запустите программу и посмотрите результат.",
    "Запишите вопрос в чат, если что-то непонятно.",
    "Повторим основные термины перед практикой.",
    "Класс содержит поля, методы и конструктор.",
    "Компилятор покажет ошибку в этой строке.",
    "Теперь сравним два способа решения задачи.",
    "В конце урока сохраним проект.",
    "Следующий пример показывает работу списка.",
    "Используйте понятные имена переменных.",
    "Не забывайте закрывать фигурные скобки.",
    "Этот алгоритм проходит по всем элементам.",
    "Домашнее задание будет связано с массивами.",
    "Спасибо, на следующем занятии продолжим тему.",
]


async def main() -> int:
    args = _parse_args()
    provider = _provider(args.provider, args.engine, fake_backend=args.fake_backend)
    languages = [item.strip() for item in args.languages.split(",") if item.strip()]
    records = []
    failures = 0
    timeouts = 0
    cold_start_ms = None

    if args.separate_cold_start and PHRASES:
        cold_records, cold_failures, cold_timeouts = await _run_phrases(provider, [PHRASES[0]], languages, phase="cold", start_index=1)
        records.extend(cold_records)
        failures += cold_failures
        timeouts += cold_timeouts
        if cold_records:
            cold_start_ms = cold_records[0]["latency_ms"]
        warm_phrases = PHRASES * max(1, int(args.warm_runs or 1))
        warm_records, warm_failures, warm_timeouts = await _run_phrases(provider, warm_phrases, languages, phase="warm", start_index=2)
        records.extend(warm_records)
        failures += warm_failures
        timeouts += warm_timeouts
    else:
        records, failures, timeouts = await _run_phrases(provider, PHRASES, languages, phase="main", start_index=1)
        for record in records:
            if record.get("ok"):
                cold_start_ms = record["latency_ms"]
                break

    provider_status = _provider_status(provider)
    engine_status = _engine_status(provider_status, args.engine)
    summary = build_summary(
        provider=args.provider,
        engine=args.engine,
        languages=languages,
        records=records,
        failures=failures,
        timeouts=timeouts or int(_metric_value(provider_status, "timeout_count") or 0),
        fallback_count=int(_metric_value(provider_status, "fallback_count") or 0),
        engine_status=engine_status,
        cold_start_ms=cold_start_ms,
        fake_backend=bool(args.fake_backend),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    output = _report_path(args)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"summary": summary, "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {output}")
    return 1 if failures else 0


async def _run_phrases(provider, phrases: list[str], languages: list[str], *, phase: str, start_index: int) -> tuple[list[dict], int, int]:
    records = []
    failures = 0
    timeouts = 0
    for offset, phrase in enumerate(phrases, start=0):
        index = start_index + offset
        started_at = perf_counter()
        try:
            translations = await provider.translate_many(phrase, "ru-RU", languages)
            latency_ms = (perf_counter() - started_at) * 1000
            record = {
                "index": index,
                "phase": phase,
                "text_preview": _truncate(phrase),
                "languages": languages,
                "translations_preview": {language: _truncate(value) for language, value in translations.items()},
                "latency_ms": latency_ms,
                "ok": True,
                "error": None,
            }
            records.append(record)
            print(f"{index:02d} {latency_ms:8.2f} ms ok {record['translations_preview']}")
        except Exception as exc:
            failures += 1
            if "timeout" in str(exc).lower() or "timeout" in exc.__class__.__name__.lower():
                timeouts += 1
            latency_ms = (perf_counter() - started_at) * 1000
            records.append(
                {
                    "index": index,
                    "phase": phase,
                    "text_preview": _truncate(phrase),
                    "languages": languages,
                    "translations_preview": {},
                    "latency_ms": latency_ms,
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {_truncate(str(exc), 180)}",
                }
            )
            print(f"{index:02d} {latency_ms:8.2f} ms fail {exc.__class__.__name__}: {_truncate(str(exc), 120)}")
    return records, failures, timeouts


def build_summary(
    *,
    provider: str,
    engine: str,
    languages: list[str],
    records: list[dict],
    failures: int,
    timeouts: int,
    fallback_count: int,
    engine_status: dict,
    cold_start_ms: float | None,
    fake_backend: bool = False,
) -> dict:
    latencies = [item["latency_ms"] for item in records if item.get("ok")]
    warm_latencies = [item["latency_ms"] for item in records if item.get("ok") and item.get("phase") != "cold"]
    return {
        "provider": provider,
        "engine": _engine_key(engine),
        "languages": languages,
        "device": engine_status.get("device"),
        "engine_status": engine_status.get("status"),
        "configured_timeout_seconds": engine_status.get("timeout_seconds") or _configured_timeout_seconds(engine),
        "cold_start_ms": cold_start_ms,
        "count": len(records),
        "successes": len(latencies),
        "failures": failures,
        "timeouts": timeouts,
        "fallback_count": fallback_count,
        "total_average_ms": mean(latencies) if latencies else None,
        "warm_average_ms": mean(warm_latencies) if warm_latencies else None,
        "warm_p50_ms": median(warm_latencies) if warm_latencies else None,
        "warm_p95_ms": _percentile(warm_latencies, 95) if warm_latencies else None,
        "average_ms": mean(latencies) if latencies else None,
        "p50_ms": median(latencies) if latencies else None,
        "p95_ms": _percentile(latencies, 95) if latencies else None,
        "sample_output_preview": _sample_preview(records),
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


def _provider(name: str, engine: str, *, fake_backend: bool = False):
    if fake_backend:
        return FakeTranslationProvider(_engine_key(engine))
    settings = get_settings()
    if name == "local":
        return create_translation_provider("local", **local_translation_provider_kwargs(settings))
    if name == "mock":
        return create_translation_provider(name)
    if name == "azure":
        return create_translation_provider(
            name,
            api_key=settings.azure_translator_key,
            region=settings.azure_translator_region,
            endpoint=settings.azure_translator_endpoint,
            api_version=settings.azure_translator_api_version,
        )
    raise SystemExit(f"Unsupported benchmark provider: {name}")


class FakeTranslationProvider(TranslationProvider):
    name = "local"

    def __init__(self, engine: str) -> None:
        self.engine = engine
        self.calls = 0

    async def translate_many(self, text: str, source_language: str, target_languages: list[str]) -> dict[str, str]:
        self.calls += 1
        return {language: f"{language}: {text[:24]}" for language in target_languages}

    def status(self) -> dict:
        return {
            "ready": True,
            "engines": {
                self.engine: {
                    "ready": True,
                    "status": "ready",
                    "device": "fake",
                    "timeout_seconds": 0,
                }
            },
            "metrics": {"fallback_count": 0, "timeout_count": 0},
        }


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((percentile / 100) * (len(ordered) - 1))
    return ordered[int(index)]


def _provider_status(provider) -> dict:
    if not hasattr(provider, "status"):
        return {}
    try:
        return provider.status()
    except Exception:
        return {}


def _engine_status(provider_status: dict, engine: str) -> dict:
    engines = provider_status.get("engines")
    if not isinstance(engines, dict):
        return {}
    status = engines.get(_engine_key(engine))
    return status if isinstance(status, dict) else {}


def _engine_key(engine: str) -> str:
    normalized = (engine or "").strip().lower().replace("-", "_")
    if normalized in {"madlad", "madlad400", "madlad_400"}:
        return "madlad400"
    if normalized in {"m2m100", "m2m100_ct2", "m2m100-ct2", "m2m100_418m", "m2m100_418m_ct2"}:
        return "m2m100_ct2"
    if normalized in {"m2m100_1_2b", "m2m100_1_2b_ct2", "m2m100_1.2b", "m2m100_1.2b_ct2"}:
        return "m2m100_1_2b_ct2"
    if normalized in {"tilmash", "issai_tilmash"}:
        return "tilmash"
    return normalized


def _metric_value(provider_status: dict, metric: str) -> int | float | None:
    metrics = provider_status.get("metrics")
    if not isinstance(metrics, dict):
        return None
    return metrics.get(metric)


def _configured_timeout_seconds(engine: str) -> float | None:
    settings = get_settings()
    key = _engine_key(engine)
    if key == "madlad400":
        return settings.madlad_timeout_seconds
    if key == "m2m100_ct2":
        return settings.m2m100_ct2_timeout_seconds
    if key == "m2m100_1_2b_ct2":
        return settings.m2m100_1_2b_ct2_timeout_seconds
    if key == "tilmash":
        return settings.tilmash_timeout_seconds
    return getattr(settings, "local_translation_timeout_seconds", None)


def _report_path(args) -> Path | None:
    if args.json_report:
        return Path(args.json_report)
    if args.write_report:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return Path("reports") / f"local_translation_benchmark_{stamp}.json"
    return None


def _sample_preview(records: list[dict]) -> dict | None:
    for item in records:
        if item.get("ok"):
            return {
                "input": _truncate(item.get("text_preview", "")),
                "translations": {key: _truncate(value) for key, value in item.get("translations_preview", {}).items()},
            }
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
    parser = argparse.ArgumentParser(description="Benchmark local translation latency with classroom/programming phrases.")
    parser.add_argument("--provider", default="local", choices=["local", "mock", "azure"])
    parser.add_argument("--engine", default="tilmash", help="Informational engine label for the report.")
    parser.add_argument("--languages", default="kk,uz", help="Comma-separated target languages.")
    parser.add_argument("--fake-backend", action="store_true", help="Use an in-process fake provider for CI/smoke only.")
    parser.add_argument("--warm-runs", type=int, default=1, help="Repeat the warm benchmark phrase set this many times.")
    parser.add_argument("--separate-cold-start", action="store_true", help="Measure the first model load/inference separately from warm latency.")
    parser.add_argument("--reuse-provider", action="store_true", help="Keep one provider instance for cold and warm runs.")
    parser.add_argument("--write-report", action="store_true", help="Write reports/local_translation_benchmark_*.json.")
    parser.add_argument("--json-report", default="", help="Write JSON report to a specific path.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
