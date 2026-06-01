import csv
import json
from pathlib import Path

import pytest

from scripts.benchmark_local_translation import build_summary
from scripts.benchmark_translation_candidates import (
    build_candidate_report,
    candidate_markdown_report,
    detect_uzbek_generation_issues,
    write_candidate_reports,
)
from scripts.benchmark_tilmash_quality import (
    QualityDatasetRow,
    apply_quality_postprocessor,
    build_quality_report,
    detect_language_mixing,
    default_flags,
    parse_quality_dataset,
    score_quality_csv,
    score_manual_csv,
    warn_if_dataset_short,
    write_score_summary_reports,
    write_quality_reports,
)


DATASET_TEXT = """№Русский (RU)Казахский (KK)Узбекский (UZ)Упрощенный китайский (zh-CN)
1Привет, класс! Откройте редактор кода.Сәлем, сынып! Код редакторын ашыңыздар.Salom, sinf! Kod muharririni oching.大家好！请打开代码编辑器。2Сегодня мы изучим переменные.Бүгін біз айнымалыларды үйренеміз.Bugun biz o'zgaruvchilarni o'rganamiz.今天我们将学习变量。3Что такое алгоритм?Алгоритм дегеніміз не?Algoritm nima?什么是算法？"""


def test_quality_dataset_parser_handles_uploaded_compact_table_format():
    rows = parse_quality_dataset(DATASET_TEXT)

    assert rows[:2] == [
        QualityDatasetRow(
            id="1",
            ru="Привет, класс! Откройте редактор кода.",
            kk="Сәлем, сынып! Код редакторын ашыңыздар.",
            uz="Salom, sinf! Kod muharririni oching.",
            zh_cn="大家好！请打开代码编辑器。",
        ),
        QualityDatasetRow(
            id="2",
            ru="Сегодня мы изучим переменные.",
            kk="Бүгін біз айнымалыларды үйренеміз.",
            uz="Bugun biz o'zgaruvchilarni o'rganamiz.",
            zh_cn="今天我们将学习变量。",
        ),
    ]


def test_quality_dataset_parser_extracts_30_rows_from_real_file_if_available():
    dataset = Path(r"C:\Users\User\Desktop\Новый текстовый документ (8).md")
    if not dataset.exists():
        pytest.skip("uploaded local dataset is not present")

    rows = parse_quality_dataset(dataset.read_text(encoding="utf-8"))

    assert len(rows) == 30
    assert rows[0].ru.startswith("Привет")
    assert rows[-1].id == "30"


def test_quality_dataset_parser_extracts_all_rows_from_canonical_csv():
    dataset = Path("data/tilmash_quality_examples.csv")

    rows = parse_quality_dataset(dataset.read_text(encoding="utf-8-sig"))

    assert len(rows) == 30
    assert rows[0] == QualityDatasetRow(
        id="1",
        ru="Привет, класс! Откройте редактор кода.",
        kk="Сәлем, сынып! Код редакторын ашыңыздар.",
        uz="Salom, sinf! Kod muharririni oching.",
        zh_cn="大家好！请打开代码编辑器。",
    )
    assert rows[9].ru == "Отладка — важная часть программирования."
    assert rows[29].zh_cn == "下课，谢谢大家！"


def test_quality_dataset_parser_supports_utf8_tsv_headers():
    text = (
        "id\tru\tkk\tuz\tzh\n"
        "1\tПривет\tСәлем\tSalom\t大家好\n"
        "2\tВопрос?\tСұрақ?\tSavol?\t问题？\n"
    )

    rows = parse_quality_dataset(text)

    assert len(rows) == 2
    assert rows[0].kk == "Сәлем"
    assert rows[1].uz == "Savol?"
    assert rows[1].zh_cn == "问题？"


def test_warns_when_dataset_has_fewer_rows_than_expected(capsys):
    warning = warn_if_dataset_short(parsed_rows=1, expected_rows=30)

    captured = capsys.readouterr()
    assert warning is not None
    assert "WARNING: Parsed only 1 rows. Expected around 30. Check dataset format." in captured.err


def test_quality_report_generation_and_outputs_are_sanitized(tmp_path):
    rows = parse_quality_dataset(DATASET_TEXT)
    report = build_quality_report(
        dataset_path=r"C:\private\models\quality_dataset.txt",
        provider="local",
        engine="tilmash",
        model_path=r"C:\private\models\tilmash",
        device="cpu",
        dtype="auto",
        language_mapping={"kk": "kaz_Cyrl"},
        forced_bos_token_ids={"kk": 256089},
        rows=[
            {
                "id": rows[0].id,
                "ru_source": rows[0].ru,
                "target_language": "kk",
                "reference_text": rows[0].kk,
                "model_output": "Сәлем, сынып! Код редакторын ашыңыздар.",
                "latency_ms": 12.5,
                "error": None,
                "manual_score": None,
                "flags": {
                    "wrong_language": False,
                    "terminology_error": False,
                    "grammar_issue": False,
                    "too_literal": False,
                    "hallucination": False,
                },
            }
        ],
        expected_rows=30,
        parsed_rows=len(rows),
        dataset_warning=None,
    )
    paths = write_quality_reports(report, tmp_path, "tilmash_quality_benchmark_test")

    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["model_path"] == "<configured>"
    assert payload["summary"]["expected_rows"] == 30
    assert payload["summary"]["parsed_rows"] == len(rows)
    assert "C:\\private" not in paths["json"].read_text(encoding="utf-8")
    assert "Manual score" in paths["md"].read_text(encoding="utf-8")
    csv_text = paths["csv"].read_text(encoding="utf-8-sig")
    assert "ru_source" in csv_text
    assert "reference_text" in csv_text
    assert "model_output" in csv_text


def test_quality_reports_include_all_kk_and_uz_rows_without_private_paths(tmp_path):
    dataset = Path("data/tilmash_quality_examples.csv")
    dataset_rows = parse_quality_dataset(dataset.read_text(encoding="utf-8-sig"))
    records = []
    for row in dataset_rows:
        for target in ("kk", "uz"):
            records.append(
                {
                    "id": row.id,
                    "ru_source": row.ru,
                    "target_language": target,
                    "reference_text": getattr(row, target),
                    "model_output": f"mock {target} {row.id}",
                    "latency_ms": 1.0,
                    "error": None,
                    "manual_score": None,
                    "flags": default_flags(),
                }
            )
    report = build_quality_report(
        dataset_path=r"C:\Users\User\Desktop\secret_dataset.csv",
        provider="mock",
        engine="tilmash",
        model_path=r"C:\private\models\tilmash",
        device="cpu",
        dtype="auto",
        language_mapping={"kk": "kaz_Cyrl", "uz": "uzn_Latn"},
        forced_bos_token_ids={"kk": 1, "uz": 2},
        rows=records,
        expected_rows=30,
        parsed_rows=len(dataset_rows),
        dataset_warning=None,
    )

    paths = write_quality_reports(report, tmp_path, "tilmash_quality_benchmark_full")

    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["summary"]["total"] == 60
    assert payload["summary"]["expected_rows"] == 30
    assert payload["summary"]["parsed_rows"] == 30
    csv_rows = list(csv.DictReader(paths["csv"].open("r", encoding="utf-8-sig", newline="")))
    assert len(csv_rows) == 60
    assert {row["target_language"] for row in csv_rows} == {"kk", "uz"}
    assert len(paths["md"].read_text(encoding="utf-8").splitlines()) >= 60
    for path in paths.values():
        text = path.read_text(encoding="utf-8-sig")
        assert "C:\\Users\\User" not in text
        assert "C:\\private" not in text


def test_score_summary_computes_target_verdicts_and_note_counts(tmp_path):
    csv_path = tmp_path / "filled_quality.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "target_language", "manual_score", "notes"])
        writer.writeheader()
        writer.writerow({"id": "1", "target_language": "kk", "manual_score": "2", "notes": "ok"})
        writer.writerow({"id": "2", "target_language": "kk", "manual_score": "1", "notes": "terminology_error grammar_issue"})
        writer.writerow({"id": "1", "target_language": "uz", "manual_score": "1", "notes": "code_mixing"})
        writer.writerow({"id": "2", "target_language": "uz", "manual_score": "0", "notes": "wrong_language hallucination"})

    summary = score_quality_csv(csv_path)

    assert summary["overall"]["score_percent"] == 50.0
    assert summary["overall"]["verdict"] == "FAIL"
    assert summary["targets"]["kk"]["score_percent"] == 75.0
    assert summary["targets"]["kk"]["verdict"] == "DEGRADED"
    assert summary["targets"]["uz"]["score_percent"] == 25.0
    assert summary["targets"]["uz"]["verdict"] == "FAIL"
    assert summary["note_counts"]["terminology_error"] == 1
    assert summary["note_counts"]["code_mixing"] == 1
    assert summary["note_counts"]["wrong_language"] == 1


def test_score_summary_reports_do_not_leak_private_paths(tmp_path):
    csv_path = tmp_path / "filled_quality.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "target_language", "manual_score", "notes"])
        writer.writeheader()
        writer.writerow({"id": "1", "target_language": "kk", "manual_score": "2", "notes": "ok"})
    summary = score_quality_csv(Path(r"C:\private\reports\filled_quality.csv"), rows=[{"target_language": "kk", "manual_score": "2", "notes": "ok"}])

    paths = write_score_summary_reports(summary, tmp_path, "tilmash_quality_score_summary_test")

    for path in paths.values():
        text = path.read_text(encoding="utf-8-sig")
        assert "C:\\private" not in text
        assert "filled_quality.csv" not in text


def test_code_mixing_detection_flags_uzbek_with_kazakh_cyrillic():
    flags = detect_language_mixing("uz", "O'zgaruvchi деректерді сақтайды va kod Git Commit")

    assert flags["contains_kazakh_cyrillic_in_uz"] is True
    assert flags["contains_cyrillic_in_uz"] is True
    assert flags["likely_code_mixing"] is True


def test_code_mixing_detection_allows_technical_latin_tokens_in_kazakh():
    flags = detect_language_mixing("kk", "Өзгерістеріңізді Git-те бекітіңіздер (Commit).")

    assert flags["contains_latin_in_kk"] is True
    assert flags["likely_code_mixing"] is False
    assert flags["wrong_language"] is False


def test_quality_postprocessor_fixes_common_kazakh_programming_terms():
    first = apply_quality_postprocessor(
        original_text="Не забудьте точку с запятой.",
        target_language="kk",
        output="Үтір нүктесін ұмытпаңыздар.",
    )
    second = apply_quality_postprocessor(
        original_text="Переменная хранит данные.",
        target_language="kk",
        output="Айнымалы деректермен бірге жүреді.",
    )

    assert first.output == "Нүктелі үтірді ұмытпаңыздар."
    assert second.output == "Айнымалы деректерді сақтайды."
    assert first.changes
    assert second.changes


def test_manual_score_csv_verdict(tmp_path):
    csv_path = tmp_path / "scores.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["manual_score"])
        writer.writeheader()
        writer.writerow({"manual_score": "2"})
        writer.writerow({"manual_score": "1"})

    result = score_manual_csv(csv_path)

    assert result["total_scored"] == 2
    assert result["score_percent"] == 75.0
    assert result["verdict"] == "DEGRADED"


def test_warm_benchmark_summary_excludes_cold_start():
    records = [
        {"latency_ms": 1000.0, "ok": True, "phase": "cold"},
        {"latency_ms": 10.0, "ok": True, "phase": "warm"},
        {"latency_ms": 20.0, "ok": True, "phase": "warm"},
    ]

    summary = build_summary(
        provider="local",
        engine="tilmash",
        languages=["kk"],
        records=records,
        failures=0,
        timeouts=0,
        fallback_count=0,
        engine_status={"device": "fake", "status": "loaded"},
        cold_start_ms=1000.0,
        fake_backend=True,
    )

    assert summary["cold_start_ms"] == 1000.0
    assert summary["warm_average_ms"] == 15.0
    assert summary["warm_p50_ms"] == 15.0
    assert summary["total_average_ms"] > summary["warm_average_ms"]


def test_candidate_benchmark_report_compares_tilmash_and_m2m100_without_private_paths(tmp_path):
    rows = [
        {
            "id": "1",
            "engine": "tilmash",
            "target_language": "uz",
            "ru_source": "Привет",
            "reference_text": "Salom",
            "model_output": "Salom",
            "latency_ms": 10.0,
            "manual_score": None,
            "fallback_used": False,
            "flags": default_flags(),
            "error": None,
        },
        {
            "id": "1",
            "engine": "m2m100_ct2",
            "target_language": "zh-Hans",
            "ru_source": "Привет",
            "reference_text": "大家好",
            "model_output": "大家好",
            "latency_ms": 20.0,
            "manual_score": None,
            "fallback_used": True,
            "flags": default_flags(),
            "error": None,
        },
    ]

    report = build_candidate_report(
        dataset_path=r"C:\private\data\tilmash_quality_examples.csv",
        targets=["uz", "zh-Hans"],
        engines=["tilmash", "m2m100_ct2"],
        rows=rows,
    )
    paths = write_candidate_reports(report, tmp_path, "translation_candidates_test")

    assert report["dataset_path"] == "<configured>"
    assert report["summary"]["by_engine_target"]["tilmash"]["uz"]["p50_latency_ms"] == 10.0
    assert report["summary"]["by_engine_target"]["m2m100_ct2"]["zh-Hans"]["fallback_used_count"] == 1
    assert report["summary"]["recommendation"] == "Collect manual scores before changing production defaults."
    assert "Manual score" in candidate_markdown_report(report)
    csv_rows = list(csv.DictReader(paths["csv"].open("r", encoding="utf-8-sig", newline="")))
    assert len(csv_rows) == 2
    for path in paths.values():
        text = path.read_text(encoding="utf-8-sig")
        assert "C:\\private" not in text


def test_uzbek_repetition_detector_flags_bilan_loop_and_technical_words_are_allowed():
    diagnostics = detect_uzbek_generation_issues("uz", "Git Commit Python bilan bilan bilan bilan ishlaydi.")

    assert diagnostics["repetition_detected"] is True
    assert diagnostics["repetition_token"] == "bilan"
    assert diagnostics["likely_bad_generation"] is True
    assert diagnostics["code_mixing_detected"] is False
    assert diagnostics["wrong_language"] is False


def test_candidate_benchmark_report_compares_tilmash_418m_and_1_2b_with_uzbek_verdict(tmp_path):
    rows = [
        {
            "id": "1",
            "engine": "tilmash",
            "target_language": "uz",
            "ru_source": "Привет",
            "reference_text": "Salom",
            "model_output": "Salom",
            "latency_ms": 10.0,
            "manual_score": None,
            "fallback_used": False,
            "flags": default_flags(),
            "diagnostics": detect_uzbek_generation_issues("uz", "Salom"),
            "error": None,
        },
        {
            "id": "1",
            "engine": "m2m100_ct2",
            "target_language": "uz",
            "ru_source": "Привет",
            "reference_text": "Salom",
            "model_output": "bilan bilan bilan bilan",
            "latency_ms": 20.0,
            "manual_score": None,
            "fallback_used": False,
            "flags": default_flags(),
            "diagnostics": detect_uzbek_generation_issues("uz", "bilan bilan bilan bilan"),
            "error": None,
        },
        {
            "id": "1",
            "engine": "m2m100_1_2b_ct2",
            "target_language": "uz",
            "ru_source": "Привет",
            "reference_text": "Salom",
            "model_output": "Salom",
            "latency_ms": 4500.0,
            "manual_score": None,
            "fallback_used": False,
            "flags": default_flags(),
            "diagnostics": detect_uzbek_generation_issues("uz", "Salom"),
            "error": None,
        },
    ]

    report = build_candidate_report(
        dataset_path=r"C:\private\data\tilmash_quality_examples.csv",
        targets=["uz"],
        engines=["tilmash", "m2m100_ct2", "m2m100_1_2b_ct2"],
        rows=rows,
    )
    paths = write_candidate_reports(report, tmp_path, "translation_candidates_uz_test")

    assert set(report["summary"]["by_engine_target"]) == {"tilmash", "m2m100_ct2", "m2m100_1_2b_ct2"}
    assert report["summary"]["by_engine_target"]["m2m100_ct2"]["uz"]["repetition_detected_count"] == 1
    assert report["summary"]["by_engine_target"]["m2m100_ct2"]["uz"]["auto_verdict"] == "FAIL"
    assert report["summary"]["by_engine_target"]["m2m100_1_2b_ct2"]["uz"]["auto_verdict"] == "DEGRADED"
    assert "repetition_detected" in paths["csv"].read_text(encoding="utf-8-sig")
    assert "M2M100_1_2B" not in paths["json"].read_text(encoding="utf-8")
    for path in paths.values():
        assert "C:\\private" not in path.read_text(encoding="utf-8-sig")
