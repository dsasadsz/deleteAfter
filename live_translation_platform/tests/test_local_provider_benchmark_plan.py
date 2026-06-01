import json
from pathlib import Path

from app.config import Settings


def test_local_provider_config_report_redacts_paths_by_default():
    from scripts.check_local_provider_config import build_config_report

    settings = Settings(
        local_translation_enabled=True,
        tilmash_enabled=True,
        tilmash_model_path="C:/private/models/tilmash",
        tilmash_tokenizer_path="C:/private/models/tilmash-tokenizer",
        local_tts_enabled=True,
        piper_enabled=True,
        piper_bin_path="C:/private/bin/piper.exe",
        piper_voice_kk="C:/private/voices/kk.onnx",
        kazakh_tts2_enabled=True,
        kazakh_tts2_model_path="C:/private/kazakh/model",
        kazakh_tts2_vocoder_path="C:/private/kazakh/vocoder",
        kazakh_tts2_tokenizer_path="C:/private/kazakh/tokenizer",
    )

    report = build_config_report(settings, verbose=False)
    encoded = json.dumps(report, ensure_ascii=False)

    assert "C:/private" not in encoded
    assert report["translation"]["engines"]["tilmash"]["status"] == "ready"
    assert report["tts"]["engines"]["piper"]["status"] == "degraded"
    assert report["tts"]["engines"]["kazakh_tts2"]["status"] == "ready"


def test_local_provider_config_report_marks_disabled_routes_and_tts_languages():
    from scripts.check_local_provider_config import build_config_report

    settings = Settings(
        local_translation_enabled=True,
        local_translation_route_kk="tilmash",
        local_translation_route_zh="m2m100_ct2",
        local_translation_route_uz="disabled",
        local_tts_enabled=True,
        local_tts_allowed_languages="kk,zh-Hans",
        local_tts_kk_engine="piper",
        local_tts_zh_engine="piper",
        local_tts_uz_engine="disabled",
        local_tts_ru_engine="disabled",
    )

    report = build_config_report(settings, verbose=False)

    assert report["translation"]["routes"]["uz"]["status"] == "disabled"
    assert report["tts"]["languages"]["uz"]["status"] == "disabled"
    assert report["tts"]["languages"]["ru"]["status"] == "disabled"


def test_local_provider_config_report_marks_experimental_uzbek_route():
    from scripts.check_local_provider_config import build_config_report

    settings = Settings(
        local_translation_enabled=True,
        local_translation_route_kk="tilmash",
        local_translation_route_zh="m2m100_ct2",
        local_translation_route_uz="m2m100_1_2b_ct2",
        m2m100_1_2b_ct2_enabled=True,
        m2m100_1_2b_ct2_model_path="C:/models/m2m100-1-2b-ct2",
        m2m100_1_2b_ct2_tokenizer_path="C:/models/m2m100-1-2b-hf",
        local_tts_enabled=True,
        local_tts_allowed_languages="kk,zh-Hans",
        local_tts_uz_engine="disabled",
    )

    report = build_config_report(settings, verbose=False)
    uz = report["translation"]["routes"]["uz"]

    assert uz["engine"] == "m2m100_1_2b_ct2"
    assert uz["status"] == "degraded"
    assert uz["experimental"] is True
    assert uz["production_ready"] is False
    assert report["tts"]["languages"]["uz"]["status"] == "disabled"


def test_local_stack_env_example_contains_profiles_without_secrets_or_private_paths():
    example = Path("docs/local_stack_env_example.md")

    text = example.read_text(encoding="utf-8")

    assert "TARGET_LANGUAGES=kk,zh-Hans" in text
    assert "strict_safe" in text
    assert "experimental_uz" in text
    assert "LOCAL_TRANSLATION_ROUTE_UZ=disabled" in text
    assert "LOCAL_TRANSLATION_ROUTE_UZ=m2m100_1_2b_ct2" in text
    assert "LOCAL_TTS_UZ_ENGINE=disabled" in text
    assert "LOCAL_TTS_RU_ENGINE=disabled" in text
    assert "MADLAD_ENABLED=false" in text
    assert "M2M100_1_2B_CT2_ENABLED=false" in text
    assert "M2M100_1_2B_CT2_ENABLED=true" in text
    assert "Uzbek quality failed automatic benchmark and requires manual review." in text
    assert "C:/private" not in text
    assert "hf_" not in text
    assert "AZURE_" not in text
    assert "ELEVENLABS_" not in text


def test_local_provider_config_report_verbose_keeps_non_secret_paths():
    from scripts.check_local_provider_config import build_config_report

    settings = Settings(tilmash_enabled=True, tilmash_model_path="C:/models/tilmash")

    report = build_config_report(settings, verbose=True)

    assert report["translation"]["engines"]["tilmash"]["config"]["model_path"] == "C:/models/tilmash"


def test_go_nogo_report_marks_missing_real_benchmarks_not_configured(tmp_path):
    from scripts.check_local_provider_config import build_config_report
    from scripts.generate_local_provider_go_nogo_report import build_go_nogo_report, write_reports

    settings = Settings()
    config_report = build_config_report(settings, verbose=False)
    report = build_go_nogo_report(config_report=config_report, benchmark_reports=[])

    assert report["conclusion"]["recommended_option"] == "NO_GO"
    assert report["translation_results"][0]["verdict"] == "NOT_CONFIGURED"
    assert report["tts_results"][0]["verdict"] == "NOT_CONFIGURED"

    markdown_path = tmp_path / "local_provider_go_nogo_report.md"
    json_path = tmp_path / "local_provider_go_nogo_report.json"
    write_reports(report, markdown_path=markdown_path, json_path=json_path)

    markdown = markdown_path.read_text(encoding="utf-8")
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert "Go/No-Go Conclusion" in markdown
    assert "NO_GO" in markdown
    assert saved["conclusion"]["recommended_option"] == "NO_GO"


def test_go_nogo_report_loader_ignores_fake_backend_reports(tmp_path):
    from scripts.generate_local_provider_go_nogo_report import load_benchmark_reports

    report_path = tmp_path / "local_tts_benchmark_fake.json"
    report_path.write_text(
        json.dumps({"summary": {"provider": "local", "engine": "kazakh_tts2", "language": "kk", "fake_backend": True}}),
        encoding="utf-8",
    )

    assert load_benchmark_reports(tmp_path) == []


def test_go_nogo_report_does_not_pass_fallback_or_not_configured_benchmark():
    from scripts.generate_local_provider_go_nogo_report import build_go_nogo_report

    config_report = {
        "translation": {"status": "not_configured", "engines": {"tilmash": {"status": "not_configured"}, "madlad400": {"status": "disabled"}}},
        "tts": {"status": "disabled", "engines": {"piper": {"status": "disabled"}, "silero": {"status": "disabled"}, "kazakh_tts2": {"status": "disabled"}}},
    }
    report = build_go_nogo_report(
        config_report=config_report,
        benchmark_reports=[
            {
                "provider": "local",
                "engine": "tilmash",
                "languages": ["kk"],
                "p95_ms": 10,
                "p50_ms": 10,
                "failures": 0,
                "timeouts": 0,
                "fallback_count": 1,
                "engine_status": "not_configured",
            }
        ],
    )

    assert report["translation_results"][0]["verdict"] == "NOT_CONFIGURED"
    assert report["conclusion"]["recommended_option"] == "NO_GO"


def test_benchmark_summaries_include_environment_and_safe_previews():
    from scripts.benchmark_local_translation import build_summary as build_translation_summary
    from scripts.benchmark_local_tts import build_summary as build_tts_summary

    translation = build_translation_summary(
        provider="local",
        engine="tilmash",
        languages=["kk"],
        records=[
            {
                "ok": True,
                "latency_ms": 100.0,
                "translations": {"kk": "қысқа мәтін"},
                "text": "Очень длинный classroom text " * 20,
            }
        ],
        failures=0,
        timeouts=0,
        fallback_count=0,
        engine_status={"device": "fake", "timeout_seconds": 1.5},
        cold_start_ms=25.0,
    )
    tts = build_tts_summary(
        engine="kazakh_tts2",
        language="kk",
        voice="kazakh_tts2-kk",
        records=[{"ok": True, "latency_ms": 120.0, "bytes": 18, "text": "Сәлем" * 20}],
        failures=0,
        timeouts=0,
        total_bytes=18,
        engine_status={"device": "fake", "timeout_seconds": 5.0, "output_format": "wav"},
        cache_report={"tested": True, "miss_latency_ms": 50.0, "hit_latency_ms": 1.0},
        fake_backend=True,
        cold_start_ms=10.0,
    )

    assert translation["environment"]["python"]
    assert translation["cold_start_ms"] == 25.0
    assert len(translation["sample_output_preview"]["input"]) <= 120
    assert tts["environment"]["platform"]
    assert tts["output_format"] == "wav"
    assert tts["cache"]["hit_latency_ms"] == 1.0
