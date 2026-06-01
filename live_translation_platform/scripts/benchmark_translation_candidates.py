from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings
from app.translation.base import TranslationProvider, create_translation_provider
from app.translation.local_provider import local_translation_provider_kwargs
from scripts.benchmark_tilmash_quality import (
    TECHNICAL_LATIN_TOKENS,
    default_flags,
    detect_language_mixing,
    parse_quality_dataset,
)


async def main() -> int:
    args = parse_args()
    dataset_path = Path(args.dataset)
    rows = parse_quality_dataset(dataset_path.read_text(encoding="utf-8-sig"))
    if args.max_rows:
        rows = rows[: args.max_rows]
    targets = [item.strip() for item in args.targets.split(",") if item.strip()]
    engines = [_engine_key(item) for item in args.engines.split(",") if item.strip()]

    records = []
    for engine in engines:
        provider = _provider_for_engine(engine, fake_backend=args.fake_backend)
        for row in rows:
            for target in targets:
                started_at = perf_counter()
                error = None
                output = ""
                fallback_before = _fallback_count(provider)
                try:
                    translations = await provider.translate_many(row.ru, "ru-RU", [target])
                    output = translations.get(target, "")
                except Exception as exc:
                    error = str(exc)[:180]
                latency_ms = (perf_counter() - started_at) * 1000
                flags = default_flags()
                flags.update(detect_language_mixing(target, output))
                diagnostics = detect_uzbek_generation_issues(target, output)
                fallback_after = _fallback_count(provider)
                records.append(
                    {
                        "id": row.id,
                        "engine": engine,
                        "target_language": target,
                        "ru_source": row.ru,
                        "reference_text": _reference_for_target(row, target),
                        "model_output": output,
                        "latency_ms": latency_ms,
                        "manual_score": None,
                        "flags": flags,
                        "diagnostics": diagnostics,
                        "fallback_used": fallback_after > fallback_before,
                        "error": error,
                    }
                )
                status = "ok" if error is None else f"error {error}"
                print(f"{engine} {row.id} {target} {latency_ms:.2f} ms {status}")

    report = build_candidate_report(
        dataset_path=str(dataset_path),
        targets=targets,
        engines=engines,
        rows=records,
    )
    if args.write_report:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        paths = write_candidate_reports(report, Path(args.output_dir), f"translation_candidates_{stamp}")
        for path in paths.values():
            print(f"wrote {path}")
    else:
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 1 if report["summary"]["failures"] else 0


def build_candidate_report(*, dataset_path: str, targets: list[str], engines: list[str], rows: list[dict[str, Any]]) -> dict:
    sanitized_rows = json.loads(json.dumps(rows, ensure_ascii=False))
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset_path": "<configured>" if dataset_path else "",
        "targets": list(targets),
        "engines": list(engines),
        "rows": sanitized_rows,
        "summary": {
            "total": len(sanitized_rows),
            "failures": sum(1 for row in sanitized_rows if row.get("error")),
            "by_engine_target": _summary_by_engine_target(sanitized_rows),
            "recommendation": "Collect manual scores before changing production defaults.",
        },
    }


def write_candidate_reports(report: dict, output_dir: Path, stem: str) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / f"{stem}.json",
        "md": output_dir / f"{stem}.md",
        "csv": output_dir / f"{stem}.csv",
    }
    paths["json"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["md"].write_text(candidate_markdown_report(report), encoding="utf-8")
    write_candidate_csv(report, paths["csv"])
    return paths


def candidate_markdown_report(report: dict) -> str:
    lines = [
        "# Translation Candidate Benchmark",
        "",
        "Manual score: 2 = good, 1 = usable with issues, 0 = wrong language or meaning lost.",
        "",
        "| Engine | Target | Row | Reference | Output | Latency ms | Fallback | Repetition | Bad generation | Manual score | Notes |",
        "| ------ | ------ | --- | --------- | ------ | ---------: | -------- | ---------- | -------------- | -----------: | ----- |",
    ]
    for row in report["rows"]:
        lines.append(
            "| {engine} | {target} | {id} | {ref} | {out} | {latency:.2f} | {fallback} | {repetition} | {bad} |  |  |".format(
                engine=_md(row.get("engine")),
                target=_md(row.get("target_language")),
                id=_md(row.get("id")),
                ref=_md(row.get("reference_text")),
                out=_md(row.get("model_output") or row.get("error")),
                latency=float(row.get("latency_ms") or 0.0),
                fallback="yes" if row.get("fallback_used") else "no",
                repetition="yes" if (row.get("diagnostics") or {}).get("repetition_detected") else "no",
                bad="yes" if (row.get("diagnostics") or {}).get("likely_bad_generation") else "no",
            )
        )
    return "\n".join(lines) + "\n"


def write_candidate_csv(report: dict, path: Path) -> None:
    fieldnames = [
        "id",
        "engine",
        "target_language",
        "ru_source",
        "reference_text",
        "model_output",
        "latency_ms",
        "manual_score",
        "wrong_language",
        "likely_code_mixing",
        "code_mixing_detected",
        "repetition_detected",
        "repetition_token",
        "likely_bad_generation",
        "fallback_used",
        "recommendation",
        "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report["rows"]:
            flags = row.get("flags") or {}
            diagnostics = row.get("diagnostics") or {}
            writer.writerow(
                {
                    "id": row.get("id"),
                    "engine": row.get("engine"),
                    "target_language": row.get("target_language"),
                    "ru_source": row.get("ru_source"),
                    "reference_text": row.get("reference_text"),
                    "model_output": row.get("model_output"),
                    "latency_ms": row.get("latency_ms"),
                    "manual_score": row.get("manual_score"),
                    "wrong_language": flags.get("wrong_language"),
                    "likely_code_mixing": flags.get("likely_code_mixing"),
                    "code_mixing_detected": diagnostics.get("code_mixing_detected"),
                    "repetition_detected": diagnostics.get("repetition_detected"),
                    "repetition_token": diagnostics.get("repetition_token"),
                    "likely_bad_generation": diagnostics.get("likely_bad_generation"),
                    "fallback_used": row.get("fallback_used"),
                    "recommendation": "manual review required",
                    "error": row.get("error"),
                }
            )


def _summary_by_engine_target(rows: list[dict[str, Any]]) -> dict:
    summary: dict[str, dict[str, dict[str, Any]]] = {}
    engines = sorted({row.get("engine") for row in rows if row.get("engine")})
    for engine in engines:
        summary[engine] = {}
        targets = sorted({row.get("target_language") for row in rows if row.get("engine") == engine and row.get("target_language")})
        for target in targets:
            bucket = [row for row in rows if row.get("engine") == engine and row.get("target_language") == target]
            ok = [row for row in bucket if not row.get("error")]
            latencies = [float(row.get("latency_ms") or 0.0) for row in ok]
            summary[engine][target] = {
                "count": len(bucket),
                "successes": len(ok),
                "failures": len(bucket) - len(ok),
                "p50_latency_ms": median(latencies) if latencies else None,
                "p95_latency_ms": _percentile(latencies, 95) if latencies else None,
                "wrong_language_count": sum(1 for row in bucket if (row.get("flags") or {}).get("wrong_language")),
                "code_mixing_count": sum(1 for row in bucket if _code_mixing_detected(row)),
                "repetition_detected_count": sum(1 for row in bucket if (row.get("diagnostics") or {}).get("repetition_detected")),
                "likely_bad_generation_count": sum(1 for row in bucket if (row.get("diagnostics") or {}).get("likely_bad_generation")),
                "fallback_used_count": sum(1 for row in bucket if row.get("fallback_used")),
                "manual_score": None,
                "auto_verdict": _auto_verdict(target, bucket, latencies),
                "recommendation": "manual review required",
            }
    return summary


def detect_uzbek_generation_issues(target_language: str, output: str) -> dict[str, Any]:
    if target_language != "uz":
        return {
            "repetition_detected": False,
            "repetition_token": None,
            "likely_bad_generation": False,
            "code_mixing_detected": False,
            "wrong_language": False,
        }
    text = output or ""
    tokens = _word_tokens(text)
    repetition_token = _repeated_token(tokens)
    length_explosion = len(tokens) > 80 or (len(text) > 500 and len(set(tokens)) < max(8, len(tokens) // 5))
    language_flags = detect_language_mixing("uz", text)
    turkishish = _turkishish_drift(tokens)
    code_mixing = bool(language_flags.get("likely_code_mixing") or language_flags.get("contains_cyrillic_in_uz") or turkishish)
    repetition_detected = repetition_token is not None
    return {
        "repetition_detected": repetition_detected,
        "repetition_token": repetition_token,
        "likely_bad_generation": bool(repetition_detected or length_explosion or code_mixing),
        "code_mixing_detected": code_mixing,
        "wrong_language": bool(language_flags.get("wrong_language")),
        "length_explosion": length_explosion,
        "turkishish_drift": turkishish,
    }


def _provider_for_engine(engine: str, *, fake_backend: bool):
    if fake_backend:
        return FakeCandidateProvider(engine)
    settings = get_settings()
    provider = create_translation_provider("local", **local_translation_provider_kwargs(settings))
    if hasattr(provider, "route_table"):
        for target in ("uz", "zh-Hans", "kk"):
            provider.route_table[target] = engine
    return provider


class FakeCandidateProvider(TranslationProvider):
    name = "local"

    def __init__(self, engine: str) -> None:
        self.engine = engine
        self.calls = 0

    async def translate_many(self, text: str, source_language: str, target_languages: list[str]) -> dict[str, str]:
        self.calls += 1
        return {language: f"{self.engine}:{language}:{text[:24]}" for language in target_languages}

    def status(self) -> dict:
        return {"metrics": {"fallback_count": 0}}


def _reference_for_target(row, target: str) -> str:
    if target == "kk":
        return row.kk
    if target == "uz":
        return row.uz
    if target in {"zh", "zh-CN", "zh-Hans"}:
        return row.zh_cn
    return ""


def _fallback_count(provider) -> int:
    if not hasattr(provider, "status"):
        return 0
    try:
        status = provider.status()
    except Exception:
        return 0
    metrics = status.get("metrics") if isinstance(status, dict) else None
    if not isinstance(metrics, dict):
        return 0
    return int(metrics.get("fallback_count") or 0)


def _percentile(values: list[float], percentile: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((percentile / 100) * (len(ordered) - 1))
    return ordered[int(index)]


def _engine_key(engine: str) -> str:
    normalized = (engine or "").strip().lower().replace("-", "_")
    if normalized in {"m2m100", "m2m100_418m", "m2m100_418m_ct2"}:
        return "m2m100_ct2"
    if normalized in {"m2m100_1_2b", "m2m100_1_2b_ct2", "m2m100_1.2b", "m2m100_1.2b_ct2"}:
        return "m2m100_1_2b_ct2"
    if normalized in {"madlad", "madlad400", "madlad_400"}:
        return "madlad400"
    return normalized


def _word_tokens(text: str) -> list[str]:
    return [token.lower().strip("'’`-") for token in re.findall(r"[A-Za-zА-Яа-яЁёІіҚқҒғҢңӨөҰұҮүҺһӘәЎў'’`-]+", text) if token.strip("'’`-")]


def _repeated_token(tokens: list[str]) -> str | None:
    previous = None
    run = 0
    counts: dict[str, int] = {}
    for token in tokens:
        if not token or token in TECHNICAL_LATIN_TOKENS:
            previous = None
            run = 0
            continue
        counts[token] = counts.get(token, 0) + 1
        if token == previous:
            run += 1
        else:
            previous = token
            run = 1
        if run >= 3:
            return token
    for token, count in counts.items():
        if count >= 6 and count / max(1, len(tokens)) >= 0.25:
            return token
    return None


def _turkishish_drift(tokens: list[str]) -> bool:
    if not tokens:
        return False
    turkish_markers = {"ve", "bir", "için", "icin", "değil", "degil", "önce", "sonra", "açın", "acın", "dosyaya"}
    hits = sum(1 for token in tokens if token in turkish_markers or "ğ" in token or "ı" in token)
    non_technical = [token for token in tokens if token not in TECHNICAL_LATIN_TOKENS]
    return hits >= 2 and hits / max(1, len(non_technical)) >= 0.12


def _code_mixing_detected(row: dict[str, Any]) -> bool:
    diagnostics = row.get("diagnostics") or {}
    flags = row.get("flags") or {}
    return bool(diagnostics.get("code_mixing_detected") or flags.get("likely_code_mixing"))


def _auto_verdict(target: str, rows: list[dict[str, Any]], latencies: list[float]) -> str:
    if target != "uz" or not rows:
        return "MANUAL_REQUIRED"
    total = len(rows)
    severe_repetition = sum(1 for row in rows if (row.get("diagnostics") or {}).get("repetition_detected"))
    code_mixing = sum(1 for row in rows if _code_mixing_detected(row))
    p95 = _percentile(latencies, 95) if latencies else None
    if severe_repetition / total > 0.10:
        return "FAIL"
    if code_mixing / total > 0.20:
        return "FAIL"
    if p95 is not None and p95 > 4000:
        return "DEGRADED"
    return "MANUAL_REQUIRED"


def _md(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare local translation candidate engines on the quality dataset.")
    parser.add_argument("--dataset", required=True, help="Path to RU/KK/UZ/zh-CN quality dataset.")
    parser.add_argument("--targets", default="uz,zh-Hans")
    parser.add_argument("--engines", default="tilmash,m2m100_ct2")
    parser.add_argument("--max-rows", type=int, default=30)
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--fake-backend", action="store_true", help="Use fake in-process translations for CI/smoke only.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
