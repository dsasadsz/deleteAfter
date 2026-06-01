from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings
from scripts.check_local_provider_config import build_config_report


TRANSLATION_EXPECTED = [
    ("tilmash", "kk"),
    ("tilmash", "uz"),
    ("madlad400", "zh-Hans"),
]
TTS_EXPECTED = [
    ("piper", "kk"),
    ("piper", "uz"),
    ("piper", "zh-Hans"),
    ("silero", "ru"),
    ("kazakh_tts2", "kk"),
]


def build_go_nogo_report(*, config_report: dict, benchmark_reports: list[dict]) -> dict:
    translation_results = _translation_results(config_report, benchmark_reports)
    tts_results = _tts_results(config_report, benchmark_reports)
    conclusion = _conclusion(translation_results, tts_results, config_report)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config_status": config_report,
        "translation_results": translation_results,
        "tts_results": tts_results,
        "conclusion": conclusion,
    }


def write_reports(report: dict, *, markdown_path: Path, json_path: Path) -> None:
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def load_benchmark_reports(reports_dir: Path) -> list[dict]:
    reports = []
    for pattern in ("local_translation_benchmark_*.json", "local_tts_benchmark_*.json"):
        for path in sorted(reports_dir.glob(pattern)):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            summary = payload.get("summary") if isinstance(payload, dict) else None
            if isinstance(summary, dict):
                if summary.get("fake_backend"):
                    continue
                summary["_source_path"] = str(path)
                reports.append(summary)
    return reports


def _translation_results(config_report: dict, benchmark_reports: list[dict]) -> list[dict]:
    rows = []
    for engine, language in TRANSLATION_EXPECTED:
        report = _find_translation_report(benchmark_reports, engine, language)
        if report is None:
            verdict = _missing_verdict(config_report["translation"]["engines"].get(engine, {}))
            rows.append(_translation_row(engine, language, None, verdict))
            continue
        rows.append(_translation_row(engine, language, report, _translation_verdict(engine, report)))
    return rows


def _tts_results(config_report: dict, benchmark_reports: list[dict]) -> list[dict]:
    rows = []
    for engine, language in TTS_EXPECTED:
        report = _find_tts_report(benchmark_reports, engine, language)
        if report is None:
            verdict = _missing_verdict(config_report["tts"]["engines"].get(engine, {}))
            rows.append(_tts_row(engine, language, None, verdict))
            continue
        rows.append(_tts_row(engine, language, report, _tts_verdict(report)))
    return rows


def _translation_row(engine: str, language: str, report: dict | None, verdict: str) -> dict:
    return {
        "engine": engine,
        "route": f"ru->{language}",
        "p50_ms": _number(report, "p50_ms"),
        "p95_ms": _number(report, "p95_ms"),
        "failures": int(_number(report, "failures") or 0),
        "timeouts": int(_number(report, "timeouts") or 0),
        "fallback_count": int(_number(report, "fallback_count") or 0),
        "cold_start_ms": _number(report, "cold_start_ms"),
        "device": report.get("device") if report else None,
        "environment": report.get("environment") if report else None,
        "source_path": report.get("_source_path") if report else None,
        "verdict": verdict,
    }


def _tts_row(engine: str, language: str, report: dict | None, verdict: str) -> dict:
    cache = report.get("cache", {}) if report else {}
    return {
        "engine": engine,
        "language": language,
        "p50_ms": _number(report, "p50_ms"),
        "p95_ms": _number(report, "p95_ms"),
        "cache_miss_p95_ms": cache.get("cache_miss_p95_ms") or cache.get("miss_latency_ms"),
        "cache_hit_p95_ms": cache.get("cache_hit_p95_ms") or cache.get("hit_latency_ms"),
        "failures": int(_number(report, "failures") or 0),
        "timeouts": int(_number(report, "timeouts") or 0),
        "cold_start_ms": _number(report, "cold_start_ms"),
        "device": report.get("device") if report else None,
        "voice": report.get("voice") if report else None,
        "output_format": report.get("output_format") if report else None,
        "environment": report.get("environment") if report else None,
        "source_path": report.get("_source_path") if report else None,
        "verdict": verdict,
    }


def _translation_verdict(engine: str, report: dict) -> str:
    if report.get("engine_status") in {"not_configured", "disabled"}:
        return str(report.get("engine_status")).upper()
    if int(report.get("fallback_count") or 0) > 0:
        return "FAIL"
    if int(report.get("failures") or 0) > 0 or int(report.get("timeouts") or 0) > 0:
        return "FAIL"
    p95 = report.get("p95_ms")
    if p95 is None:
        return "NOT_CONFIGURED"
    if engine == "tilmash":
        if p95 <= 700:
            return "PASS"
        if p95 <= 1500:
            return "DEGRADED"
        return "FAIL"
    if p95 <= 2500:
        return "PASS"
    if p95 <= 4000:
        return "DEGRADED"
    return "FAIL"


def _tts_verdict(report: dict) -> str:
    if report.get("engine_status") in {"not_configured", "disabled"}:
        return str(report.get("engine_status")).upper()
    if int(report.get("failures") or 0) > 0 or int(report.get("timeouts") or 0) > 0:
        return "FAIL"
    p95 = report.get("p95_ms")
    cache = report.get("cache") or {}
    hit_p95 = cache.get("cache_hit_p95_ms") or cache.get("hit_latency_ms")
    if p95 is None:
        return "NOT_CONFIGURED"
    if hit_p95 is not None and hit_p95 > 200:
        return "DEGRADED"
    if p95 <= 3000:
        return "PASS"
    if p95 <= 6000:
        return "DEGRADED"
    return "FAIL"


def _missing_verdict(config: dict) -> str:
    status = config.get("status")
    if status == "ready":
        return "NO_BENCHMARK"
    if status == "degraded":
        return "NO_BENCHMARK_DEGRADED_CONFIG"
    if status == "disabled":
        return "DISABLED"
    return "NOT_CONFIGURED"


def _conclusion(translation_results: list[dict], tts_results: list[dict], config_report: dict) -> dict:
    translation_ok = all(row["verdict"] in {"PASS", "DEGRADED"} for row in translation_results)
    fast_tts = [row for row in tts_results if not (row["engine"] == "kazakh_tts2" and row["language"] == "kk")]
    fast_tts_ok = all(row["verdict"] in {"PASS", "DEGRADED"} for row in fast_tts)
    kazakh_quality = next((row for row in tts_results if row["engine"] == "kazakh_tts2" and row["language"] == "kk"), None)
    kazakh_ok = kazakh_quality is not None and kazakh_quality["verdict"] in {"PASS", "DEGRADED"}
    if not translation_ok:
        option = "NO_GO"
        defaults = {}
    elif fast_tts_ok and kazakh_ok:
        option = "Option C"
        defaults = OPTION_C_DEFAULTS
    elif fast_tts_ok:
        option = "Option B"
        defaults = OPTION_B_DEFAULTS
    else:
        option = "Option A"
        defaults = OPTION_A_DEFAULTS
    return {
        "recommended_option": option,
        "recommended_defaults": defaults,
        "local_mode_safe_for_demo": option in {"Option A", "Option B", "Option C"},
        "local_mode_safe_for_production": False,
        "cloud_fallback": "mock/external fallback remains configured; do not remove Azure/mock providers.",
        "gpu_required": _gpu_required(config_report),
        "needs_more_testing": [
            "Run with real local model files or local model servers.",
            "Repeat benchmarks under concurrent classroom load.",
            "Review sample output quality with native speakers before production.",
        ],
    }


OPTION_A_DEFAULTS = {
    "TRANSLATION_PROVIDER": "local",
    "LOCAL_TRANSLATION_ENABLED": "true",
    "LOCAL_TRANSLATION_ROUTING_ENABLED": "true",
    "LOCAL_TRANSLATION_DEFAULT_ENGINE": "tilmash",
    "LOCAL_TRANSLATION_FALLBACK_ENGINE": "mock",
    "TTS_PROVIDER": "azure",
}
OPTION_B_DEFAULTS = {
    **{key: value for key, value in OPTION_A_DEFAULTS.items() if key != "TTS_PROVIDER"},
    "TTS_PROVIDER": "local",
    "LOCAL_TTS_ENABLED": "true",
    "LOCAL_TTS_DEFAULT_ENGINE": "piper",
    "LOCAL_TTS_RU_ENGINE": "silero",
    "LOCAL_TTS_KK_ENGINE": "piper",
    "LOCAL_TTS_UZ_ENGINE": "piper",
    "LOCAL_TTS_ZH_ENGINE": "piper",
}
OPTION_C_DEFAULTS = {
    **OPTION_B_DEFAULTS,
    "LOCAL_TTS_KK_ENGINE": "kazakh_tts2",
    "KAZAKH_TTS2_ENABLED": "true",
}


def _find_translation_report(reports: list[dict], engine: str, language: str) -> dict | None:
    for report in reversed(reports):
        if report.get("provider") != "local":
            continue
        if _engine_key(report.get("engine")) != engine:
            continue
        if language in (report.get("languages") or []):
            return report
    return None


def _find_tts_report(reports: list[dict], engine: str, language: str) -> dict | None:
    for report in reversed(reports):
        if report.get("provider") != "local":
            continue
        if _engine_key(report.get("engine")) == engine and report.get("language") == language:
            return report
    return None


def _number(report: dict | None, key: str) -> float | int | None:
    if not report:
        return None
    value = report.get(key)
    return value if isinstance(value, int | float) else None


def _engine_key(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"madlad", "madlad400", "madlad_400"}:
        return "madlad400"
    return normalized


def _gpu_required(config_report: dict) -> list[str]:
    required = []
    for section in ("translation", "tts"):
        for name, engine in config_report.get(section, {}).get("engines", {}).items():
            device = str(engine.get("config", {}).get("device") or "")
            if device.startswith("cuda") and engine.get("status") in {"ready", "degraded", "NO_BENCHMARK"}:
                required.append(name)
    return required


def _markdown(report: dict) -> str:
    lines = [
        "# Local Provider Go/No-Go Report",
        "",
        f"Generated: {report['created_at']}",
        "",
        "## Translation Results",
        "",
        "| Engine | Route | p50 | p95 | Failures | Timeouts | Verdict |",
        "| ------ | ----- | --: | --: | -------: | -------: | ------- |",
    ]
    for row in report["translation_results"]:
        lines.append(
            f"| {row['engine']} | {row['route']} | {_fmt_ms(row['p50_ms'])} | {_fmt_ms(row['p95_ms'])} | {row['failures']} | {row['timeouts']} | {row['verdict']} |"
        )
    lines.extend(
        [
            "",
            "## TTS Results",
            "",
            "| Engine | Language | Cache miss p95 | Cache hit p95 | Failures | Timeouts | Verdict |",
            "| ------ | -------- | -------------: | ------------: | -------: | -------: | ------- |",
        ]
    )
    for row in report["tts_results"]:
        lines.append(
            f"| {row['engine']} | {row['language']} | {_fmt_ms(row['cache_miss_p95_ms'])} | {_fmt_ms(row['cache_hit_p95_ms'])} | {row['failures']} | {row['timeouts']} | {row['verdict']} |"
        )
    conclusion = report["conclusion"]
    lines.extend(
        [
            "",
            "## Recommended Defaults",
            "",
            f"Recommended option: `{conclusion['recommended_option']}`",
            "",
            "```env",
        ]
    )
    for key, value in conclusion["recommended_defaults"].items():
        lines.append(f"{key}={value}")
    lines.extend(
        [
            "```",
            "",
            "## Go/No-Go Conclusion",
            "",
            f"- Safe for demo: {conclusion['local_mode_safe_for_demo']}",
            f"- Safe for production: {conclusion['local_mode_safe_for_production']}",
            f"- Cloud fallback: {conclusion['cloud_fallback']}",
            f"- GPU required: {', '.join(conclusion['gpu_required']) or 'none detected from config'}",
            "- More testing:",
        ]
    )
    for item in conclusion["needs_more_testing"]:
        lines.append(f"  - {item}")
    return "\n".join(lines) + "\n"


def _fmt_ms(value: object) -> str:
    if isinstance(value, int | float):
        return f"{value:.2f}"
    return "-"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate consolidated local provider go/no-go report.")
    parser.add_argument("--reports-dir", default="reports", help="Directory containing local benchmark JSON reports.")
    parser.add_argument("--markdown", default="reports/local_provider_go_nogo_report.md")
    parser.add_argument("--json", default="reports/local_provider_go_nogo_report.json")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    config_report = build_config_report(get_settings(), verbose=False)
    benchmark_reports = load_benchmark_reports(Path(args.reports_dir))
    report = build_go_nogo_report(config_report=config_report, benchmark_reports=benchmark_reports)
    write_reports(report, markdown_path=Path(args.markdown), json_path=Path(args.json))
    print(f"wrote {args.markdown}")
    print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
