from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings
from app.glossary.default_glossaries import PROGRAMMING_RU
from app.glossary.postprocessor import TranslationPostProcessor
from app.glossary.schemas import GlossaryTermData
from app.translation.base import create_translation_provider
from app.translation.local_engines.model_loader import (
    _forced_bos_token_id,
    resolve_tilmash_language_code,
    sanitize_error_message,
)
from app.translation.local_provider import local_translation_provider_kwargs


@dataclass(frozen=True)
class QualityDatasetRow:
    id: str
    ru: str
    kk: str
    uz: str
    zh_cn: str


@dataclass(frozen=True)
class QualityPostprocessResult:
    output: str
    changes: list[dict]


DATASET_WARNING_TEMPLATE = "WARNING: Parsed only {parsed_rows} rows. Expected around {expected_rows}. Check dataset format."
QUALITY_NOTES = ("wrong_language", "terminology_error", "grammar_issue", "too_literal", "hallucination", "code_mixing")
KAZAKH_CYRILLIC_LETTERS = set("әіңғқұүөһӘІҢҒҚҰҮӨҺ")
TECHNICAL_LATIN_TOKENS = {
    "api",
    "asp",
    "backend",
    "c",
    "code",
    "commit",
    "css",
    "frontend",
    "git",
    "html",
    "javascript",
    "js",
    "json",
    "net",
    "python",
    "sql",
    "typescript",
    "ts",
    "url",
    "visual",
}


async def main() -> int:
    args = parse_args()
    if args.score_file:
        summary = score_quality_csv(Path(args.score_file))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        paths = write_score_summary_reports(summary, Path(args.output_dir), f"tilmash_quality_score_summary_{stamp}")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        for path in paths.values():
            print(f"wrote {path}")
        return 0
    if not args.dataset:
        print("--dataset is required unless --score-file is used", file=sys.stderr)
        return 2

    dataset_path = Path(args.dataset)
    rows = parse_quality_dataset(dataset_path.read_text(encoding="utf-8-sig"))
    parsed_rows = len(rows)
    dataset_warning = warn_if_dataset_short(parsed_rows=parsed_rows, expected_rows=args.expected_rows)
    if args.max_rows:
        rows = rows[: args.max_rows]
    targets = [item.strip() for item in args.targets.split(",") if item.strip()]
    provider = quality_provider(args.provider, args.engine)
    settings = get_settings()

    for _ in range(max(0, int(args.warmup or 0))):
        if rows:
            await provider.translate_many(rows[0].ru, "ru-RU", targets[:1] or ["kk"])

    records = []
    for row in rows:
        for target in targets:
            reference = getattr(row, target, "")
            started_at = perf_counter()
            try:
                translations = await provider.translate_many(row.ru, "ru-RU", [target])
                raw_output = translations.get(target, "")
                error = None
            except Exception as exc:
                raw_output = ""
                error = sanitize_error_message(exc, settings.tilmash_model_path, settings.tilmash_tokenizer_path)
            postprocessed = (
                apply_quality_postprocessor(original_text=row.ru, target_language=target, output=raw_output)
                if args.apply_postprocessor and error is None
                else QualityPostprocessResult(output=raw_output, changes=[])
            )
            output = postprocessed.output
            flags = default_flags()
            flags.update(detect_language_mixing(target, output))
            if postprocessed.changes:
                flags["terminology_error"] = True
            latency_ms = (perf_counter() - started_at) * 1000
            records.append(
                {
                    "id": row.id,
                    "ru_source": row.ru,
                    "target_language": target,
                    "reference_text": reference,
                    "raw_model_output": raw_output,
                    "postprocessed_output": output,
                    "model_output": output,
                    "latency_ms": latency_ms,
                    "error": error,
                    "manual_score": None,
                    "notes": "",
                    "flags": flags,
                    "postprocess_changes": postprocessed.changes,
                }
            )
            status = "ok" if error is None else f"error {error}"
            print(f"{row.id} {target} {latency_ms:.2f} ms {status}")

    language_mapping, forced_bos = tilmash_language_metadata(targets)
    report = build_quality_report(
        dataset_path=str(dataset_path),
        provider=args.provider,
        engine=args.engine,
        model_path=settings.tilmash_model_path,
        device=settings.tilmash_device,
        dtype=settings.tilmash_dtype,
        language_mapping=language_mapping,
        forced_bos_token_ids=forced_bos,
        rows=records,
        expected_rows=args.expected_rows,
        parsed_rows=parsed_rows,
        dataset_warning=dataset_warning,
    )
    if args.write_report:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        paths = write_quality_reports(report, Path(args.output_dir), f"tilmash_quality_benchmark_{stamp}")
        for path in paths.values():
            print(f"wrote {path}")
    else:
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if report["summary"]["failures"]:
        return 1
    if dataset_warning and args.strict_dataset:
        return 1
    return 0


def parse_quality_dataset(text: str) -> list[QualityDatasetRow]:
    normalized = text.replace("\ufeff", "").strip()
    rows = _parse_delimited_table(normalized)
    if rows:
        return rows
    if "|" in normalized:
        rows = _parse_markdown_table(normalized)
        if rows:
            return rows
    return _parse_compact_table(normalized)


def warn_if_dataset_short(*, parsed_rows: int, expected_rows: int | None) -> str | None:
    if not expected_rows or parsed_rows >= expected_rows:
        return None
    warning = DATASET_WARNING_TEMPLATE.format(parsed_rows=parsed_rows, expected_rows=expected_rows)
    print(warning, file=sys.stderr)
    return warning


def build_quality_report(
    *,
    dataset_path: str,
    provider: str,
    engine: str,
    model_path: str,
    device: str,
    dtype: str,
    language_mapping: dict[str, str],
    forced_bos_token_ids: dict[str, int | None],
    rows: list[dict[str, Any]],
    expected_rows: int | None = None,
    parsed_rows: int | None = None,
    dataset_warning: str | None = None,
) -> dict:
    successes = [row for row in rows if not row.get("error")]
    latencies = [float(row["latency_ms"]) for row in successes]
    sanitized_rows = json.loads(json.dumps(rows, ensure_ascii=False))
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset_path": "<configured>" if dataset_path else "",
        "provider": provider,
        "engine": engine,
        "model_path": "<configured>" if model_path else "",
        "device": device,
        "dtype": dtype,
        "target_language_mapping": dict(language_mapping),
        "forced_bos_token_ids": dict(forced_bos_token_ids),
        "rows": sanitized_rows,
        "summary": {
            "total": len(rows),
            "successes": len(successes),
            "failures": len(rows) - len(successes),
            "expected_rows": expected_rows,
            "parsed_rows": parsed_rows,
            "source_rows": parsed_rows,
            "dataset_warning": dataset_warning,
            "average_latency_ms": mean(latencies) if latencies else None,
            "p50_latency_ms": median(latencies) if latencies else None,
            "p95_latency_ms": percentile(latencies, 95) if latencies else None,
            "wrong_language_count": None,
            "likely_code_mixing_count": sum(1 for row in sanitized_rows if (row.get("flags") or {}).get("likely_code_mixing")),
        },
    }


def write_quality_reports(report: dict, output_dir: Path, stem: str) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / f"{stem}.json",
        "md": output_dir / f"{stem}.md",
        "csv": output_dir / f"{stem}.csv",
    }
    paths["json"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["md"].write_text(markdown_report(report), encoding="utf-8")
    write_csv_report(report, paths["csv"])
    return paths


def markdown_report(report: dict) -> str:
    lines = [
        "# Tilmash Quality Benchmark",
        "",
        "Manually fill `Manual score`: 2 = good / meaning preserved, 1 = understandable but has issues, 0 = bad / wrong language / meaning lost.",
        "Use `Notes` for: wrong_language, terminology_error, grammar_issue, too_literal, hallucination, code_mixing.",
        "",
        "| # | Target | RU | Reference | Raw model output | Postprocessed output | Latency ms | Manual score | Notes |",
        "| - | ------ | -- | --------- | ---------------- | -------------------- | ---------: | -----------: | ----- |",
    ]
    for row in report["rows"]:
        lines.append(
            "| {id} | {target} | {ru} | {ref} | {raw} | {out} | {latency:.2f} |  |  |".format(
                id=_md(row.get("id")),
                target=_md(row.get("target_language")),
                ru=_md(row.get("ru_source")),
                ref=_md(row.get("reference_text")),
                raw=_md(row.get("raw_model_output", row.get("model_output")) or row.get("error")),
                out=_md(row.get("postprocessed_output", row.get("model_output")) or row.get("error")),
                latency=float(row.get("latency_ms") or 0.0),
            )
        )
    return "\n".join(lines) + "\n"


def write_csv_report(report: dict, path: Path) -> None:
    fieldnames = [
        "id",
        "target_language",
        "ru_source",
        "reference_text",
        "raw_model_output",
        "postprocessed_output",
        "model_output",
        "latency_ms",
        "manual_score",
        "notes",
        "wrong_language",
        "terminology_error",
        "grammar_issue",
        "too_literal",
        "hallucination",
        "contains_kazakh_cyrillic_in_uz",
        "contains_cyrillic_in_uz",
        "contains_latin_in_kk",
        "likely_code_mixing",
        "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report["rows"]:
            flags = row.get("flags") or {}
            writer.writerow(
                {
                    "id": row.get("id"),
                    "target_language": row.get("target_language"),
                    "ru_source": row.get("ru_source"),
                    "reference_text": row.get("reference_text"),
                    "raw_model_output": row.get("raw_model_output", row.get("model_output")),
                    "postprocessed_output": row.get("postprocessed_output", row.get("model_output")),
                    "model_output": row.get("model_output"),
                    "latency_ms": row.get("latency_ms"),
                    "manual_score": row.get("manual_score"),
                    "notes": row.get("notes", ""),
                    "wrong_language": flags.get("wrong_language"),
                    "terminology_error": flags.get("terminology_error"),
                    "grammar_issue": flags.get("grammar_issue"),
                    "too_literal": flags.get("too_literal"),
                    "hallucination": flags.get("hallucination"),
                    "contains_kazakh_cyrillic_in_uz": flags.get("contains_kazakh_cyrillic_in_uz"),
                    "contains_cyrillic_in_uz": flags.get("contains_cyrillic_in_uz"),
                    "contains_latin_in_kk": flags.get("contains_latin_in_kk"),
                    "likely_code_mixing": flags.get("likely_code_mixing"),
                    "error": row.get("error"),
                }
            )


def score_manual_csv(path: Path) -> dict:
    summary = score_quality_csv(path)
    overall = summary["overall"]
    return {
        "score_file": "<configured>",
        "total_scored": overall["scored_count"],
        "score_sum": overall["score_sum"],
        "score_percent": overall["score_percent"],
        "verdict": overall["verdict"],
    }


def score_quality_csv(path: Path, rows: list[dict[str, str]] | None = None) -> dict:
    if rows is None:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    scored: list[tuple[str, int, set[str]]] = []
    note_counts = {note: 0 for note in QUALITY_NOTES}
    for row in rows:
        value = (row.get("manual_score") or "").strip()
        notes = _note_tokens(row.get("notes") or "")
        for note in notes:
            if note in note_counts:
                note_counts[note] += 1
        if value == "":
            continue
        score = max(0, min(2, int(float(value))))
        scored.append(((row.get("target_language") or "unknown").strip() or "unknown", score, notes))
    targets: dict[str, dict] = {}
    for target in sorted({target for target, _, _ in scored}):
        target_scores = [score for item_target, score, _ in scored if item_target == target]
        targets[target] = _score_bucket(target_scores)
        targets[target]["recommendation"] = _quality_recommendation(target, targets[target]["verdict"])
    overall_scores = [score for _, score, _ in scored]
    overall = _score_bucket(overall_scores)
    overall["recommendation"] = _quality_recommendation("overall", overall["verdict"])
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "score_file": "<configured>",
        "overall": overall,
        "targets": targets,
        "note_counts": note_counts,
    }


def write_score_summary_reports(summary: dict, output_dir: Path, stem: str) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": output_dir / f"{stem}.json",
        "md": output_dir / f"{stem}.md",
    }
    paths["json"].write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["md"].write_text(markdown_score_summary(summary), encoding="utf-8")
    return paths


def markdown_score_summary(summary: dict) -> str:
    lines = [
        "# Tilmash Quality Score Summary",
        "",
        "| Scope | Scored | Score | Percent | Verdict | Recommendation |",
        "| ----- | -----: | ----: | ------: | ------- | -------------- |",
    ]
    overall = summary["overall"]
    lines.append(
        f"| overall | {overall['scored_count']} | {overall['score_sum']} | {_percent(overall['score_percent'])} | {overall['verdict']} | {_md(overall['recommendation'])} |"
    )
    for target, bucket in sorted(summary["targets"].items()):
        lines.append(
            f"| {target} | {bucket['scored_count']} | {bucket['score_sum']} | {_percent(bucket['score_percent'])} | {bucket['verdict']} | {_md(bucket['recommendation'])} |"
        )
    lines.extend(["", "## Note Counts", "", "| Note | Count |", "| ---- | ----: |"])
    for note, count in summary["note_counts"].items():
        lines.append(f"| {note} | {count} |")
    return "\n".join(lines) + "\n"


def _score_bucket(scores: list[int]) -> dict:
    score_percent = (sum(scores) / (2 * len(scores)) * 100) if scores else None
    return {
        "scored_count": len(scores),
        "score_sum": sum(scores),
        "score_percent": score_percent,
        "verdict": score_verdict(score_percent),
    }


def score_verdict(score_percent: float | None) -> str:
    if score_percent is None:
        return "UNSCORED"
    if score_percent >= 80:
        return "PASS"
    if score_percent >= 60:
        return "DEGRADED"
    return "FAIL"


def _quality_recommendation(target: str, verdict: str) -> str:
    if verdict == "PASS":
        return "Enable with monitoring."
    if verdict == "DEGRADED":
        return "Keep experimental; use glossary/postprocessor and monitor manual scores."
    if verdict == "FAIL" and target == "uz":
        return "Do not enable for production; use fallback or a separate Uzbek model."
    if verdict == "FAIL":
        return "Do not enable for production until quality improves."
    return "Collect manual scores before enabling."


def _percent(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}%"


def _note_tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[\s,;|]+", value.strip().lower()) if token}


def detect_language_mixing(target_language: str, output: str) -> dict[str, bool]:
    text = output or ""
    target = (target_language or "").strip()
    cyrillic_count = len(re.findall(r"[А-Яа-яЁёӘәІіҢңҒғҚқҰұҮүӨөҺһ]", text))
    kazakh_specific_count = sum(1 for char in text if char in KAZAKH_CYRILLIC_LETTERS)
    latin_text = _without_technical_latin_tokens(text)
    latin_count = len(re.findall(r"[A-Za-z]", latin_text))
    has_latin_words = bool(re.search(r"[A-Za-z]", latin_text))
    flags = {
        "contains_kazakh_cyrillic_in_uz": False,
        "contains_cyrillic_in_uz": False,
        "contains_latin_in_kk": False,
        "likely_code_mixing": False,
        "wrong_language": False,
    }
    if target == "uz":
        flags["contains_kazakh_cyrillic_in_uz"] = kazakh_specific_count > 0
        flags["contains_cyrillic_in_uz"] = cyrillic_count > 0
        flags["likely_code_mixing"] = flags["contains_kazakh_cyrillic_in_uz"] or (cyrillic_count > 0 and has_latin_words)
        flags["wrong_language"] = flags["contains_kazakh_cyrillic_in_uz"]
    elif target == "kk":
        flags["contains_latin_in_kk"] = bool(re.search(r"[A-Za-z]", text))
        mostly_latin = latin_count > 0 and latin_count > max(8, cyrillic_count * 2)
        flags["likely_code_mixing"] = mostly_latin
        flags["wrong_language"] = mostly_latin
    return flags


def apply_quality_postprocessor(*, original_text: str, target_language: str, output: str) -> QualityPostprocessResult:
    result = TranslationPostProcessor().postprocess(
        original_text,
        {target_language: output},
        default_quality_glossary_terms(),
    )
    return QualityPostprocessResult(output=result.translations.get(target_language, output), changes=result.changes)


def default_quality_glossary_terms() -> list[GlossaryTermData]:
    terms = []
    for index, item in enumerate(PROGRAMMING_RU["terms"]):
        terms.append(
            GlossaryTermData(
                id=f"default_programming_{index}",
                source=item["source"],
                canonical=item["canonical"],
                aliases=list(item.get("aliases") or []),
                translations=dict(item.get("translations") or {}),
                case_sensitive=bool(item.get("case_sensitive", False)),
                match_type=item.get("match_type", "phrase"),
                priority=int(item.get("priority", 0)),
                enabled=bool(item.get("enabled", True)),
            )
        )
    return terms


def _without_technical_latin_tokens(text: str) -> str:
    def replace(match: re.Match) -> str:
        return " " if match.group(0).lower() in TECHNICAL_LATIN_TOKENS else match.group(0)

    return re.sub(r"[A-Za-z]+", replace, text)


def quality_provider(provider: str, engine: str):
    if provider == "local":
        settings = get_settings()
        return create_translation_provider("local", **local_translation_provider_kwargs(settings))
    if provider == "mock":
        return create_translation_provider("mock")
    raise SystemExit(f"Unsupported provider: {provider}")


def tilmash_language_metadata(targets: list[str]) -> tuple[dict[str, str], dict[str, int | None]]:
    try:
        from transformers import AutoTokenizer
    except Exception:
        return {}, {target: None for target in targets}
    settings = get_settings()
    tokenizer_path = settings.tilmash_tokenizer_path or settings.tilmash_model_path
    if not tokenizer_path:
        return {}, {target: None for target in targets}
    try:
        tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True)
    except Exception:
        return {}, {target: None for target in targets}
    mapping = {}
    forced_bos = {}
    for target in targets:
        try:
            mapping[target] = resolve_tilmash_language_code(target, tokenizer=tokenizer)
            forced_bos[target] = _forced_bos_token_id(tokenizer, target)
        except Exception:
            forced_bos[target] = None
    return mapping, forced_bos


def default_flags() -> dict[str, bool]:
    return {
        "wrong_language": False,
        "terminology_error": False,
        "grammar_issue": False,
        "too_literal": False,
        "hallucination": False,
        "contains_kazakh_cyrillic_in_uz": False,
        "contains_cyrillic_in_uz": False,
        "contains_latin_in_kk": False,
        "likely_code_mixing": False,
    }


def percentile(values: list[float], percentile_value: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((percentile_value / 100) * (len(ordered) - 1))
    return ordered[int(index)]


def _parse_markdown_table(text: str) -> list[QualityDatasetRow]:
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 5 or cells[0] in {"№", "#"}:
            continue
        rows.append(QualityDatasetRow(cells[0], cells[1], cells[2], cells[3], cells[4]))
    return rows


def _parse_delimited_table(text: str) -> list[QualityDatasetRow]:
    if not text:
        return []
    sample = text[:4096]
    delimiters = [",", "\t"]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        delimiters.insert(0, dialect.delimiter)
    except csv.Error:
        pass
    for delimiter in dict.fromkeys(delimiters):
        rows = _parse_delimited_table_with_delimiter(text, delimiter)
        if rows:
            return rows
    return []


def _parse_delimited_table_with_delimiter(text: str, delimiter: str) -> list[QualityDatasetRow]:
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        return []
    field_map = _quality_dataset_field_map(reader.fieldnames)
    if not all(field_map.get(name) for name in ("id", "ru", "kk", "uz", "zh_cn")):
        return []
    rows = []
    for item in reader:
        row = QualityDatasetRow(
            id=(item.get(field_map["id"]) or "").strip(),
            ru=(item.get(field_map["ru"]) or "").strip(),
            kk=(item.get(field_map["kk"]) or "").strip(),
            uz=(item.get(field_map["uz"]) or "").strip(),
            zh_cn=(item.get(field_map["zh_cn"]) or "").strip(),
        )
        if all((row.id, row.ru, row.kk, row.uz, row.zh_cn)):
            rows.append(row)
    return rows


def _quality_dataset_field_map(fieldnames: list[str]) -> dict[str, str]:
    aliases = {
        "id": {"id", "№", "#", "number", "no"},
        "ru": {"ru", "русский", "русский (ru)", "russian"},
        "kk": {"kk", "казахский", "казахский (kk)", "kazakh"},
        "uz": {"uz", "узбекский", "узбекский (uz)", "uzbek"},
        "zh_cn": {"zh", "zh-cn", "zh_cn", "упрощенный китайский", "упрощенный китайский (zh-cn)", "chinese"},
    }
    mapped: dict[str, str] = {}
    for fieldname in fieldnames:
        normalized = (fieldname or "").strip().lower().replace("\ufeff", "")
        for canonical, names in aliases.items():
            if normalized in names:
                mapped[canonical] = fieldname
    return mapped


def _parse_compact_table(text: str) -> list[QualityDatasetRow]:
    body = re.sub(r"^№\s*Русский \(RU\)\s*Казахский \(KK\)\s*Узбекский \(UZ\)\s*Упрощенный китайский \(zh-CN\)", "", text)
    matches = list(re.finditer(r"(?<!\d)(\d{1,3})(?=[А-ЯЁA-Z])", body))
    rows = []
    for index, match in enumerate(matches):
        row_id = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        chunk = body[start:end].strip()
        parsed = _parse_compact_row(row_id, chunk)
        if parsed:
            rows.append(parsed)
    return rows


def _parse_compact_row(row_id: str, chunk: str) -> QualityDatasetRow | None:
    han_match = re.search(r"[\u4e00-\u9fff]", chunk)
    if not han_match:
        return None
    before_zh = chunk[: han_match.start()]
    zh_cn = chunk[han_match.start() :].strip()
    uz_boundaries = list(re.finditer(r"[.!?](?=[A-Za-zOʻ'])", before_zh))
    if not uz_boundaries:
        return None
    uz_start = uz_boundaries[-1].end()
    cyrillic = before_zh[:uz_start]
    uz = before_zh[uz_start:].strip()
    parts = _split_cyrillic_columns(cyrillic)
    if len(parts) != 2:
        return None
    return QualityDatasetRow(row_id, parts[0], parts[1], uz, zh_cn)


def _split_cyrillic_columns(text: str) -> list[str]:
    candidates: list[tuple[float, str, str]] = []
    for index, char in enumerate(text):
        if char not in ".!?":
            continue
        left = text[: index + 1].strip()
        right = text[index + 1 :].strip()
        if left and right:
            candidates.append((_kazakh_likelihood(right) - (_kazakh_likelihood(left) * 0.25), left, right))
    if not candidates:
        return []
    _, left, right = max(candidates, key=lambda item: item[0])
    return [left, right]


def _kazakh_likelihood(text: str) -> float:
    lowered = text.lower()
    specific = sum(1 for char in lowered if char in "әғқңөұүіһ")
    early_specific = sum(1 for char in lowered[:24] if char in "әғқңөұүіһ")
    word_hits = sum(
        lowered.count(word)
        for word in (
            "дың",
            "дің",
            "тың",
            "тің",
            "ңыз",
            "ңіз",
            "мыз",
            "міз",
            "дар",
            "дер",
            "лар",
            "лер",
            "ды",
            "ді",
            "ты",
            "ті",
        )
    )
    first_word = re.match(r"[а-яёәғқңөұүіһ]+", lowered)
    starts_like_plain_russian = bool(first_word and not any(char in "әғқңөұүіһ" for char in first_word.group(0)))
    return specific * 3.0 + early_specific * 8.0 + word_hits - (8.0 if starts_like_plain_russian else 0.0)


def _md(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Tilmash quality against RU/KK/UZ reference examples.")
    parser.add_argument("--dataset", default="", help="Path to RU/KK/UZ/zh-CN quality dataset.")
    parser.add_argument("--targets", default="kk,uz")
    parser.add_argument("--provider", default="local", choices=["local", "mock"])
    parser.add_argument("--engine", default="tilmash")
    parser.add_argument("--max-rows", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--manual-template", action="store_true", help="Reserved for compatibility; reports always include manual score columns.")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--score-file", default="", help="Score an edited CSV report instead of running translation.")
    parser.add_argument("--expected-rows", type=int, default=30)
    parser.add_argument("--strict-dataset", action="store_true", help="Exit non-zero when parsed rows are below --expected-rows.")
    parser.add_argument("--apply-postprocessor", action="store_true", help="Apply default quality glossary/postprocessor to model outputs before reporting.")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
