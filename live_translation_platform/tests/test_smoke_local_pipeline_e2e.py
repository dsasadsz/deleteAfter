from types import SimpleNamespace

from app.tts.base import TTSResult
from scripts.smoke_local_pipeline_e2e import (
    aggregate_report,
    build_verdict,
    local_translation_status_summary,
    sanitize_report,
)


def test_aggregate_report_marks_pass_without_fallback():
    report = aggregate_report(
        options=SimpleNamespace(
            audio_wav="sample.wav",
            stt_provider="faster_whisper",
            translation_provider="local",
            translation_fallback="mock",
            target_languages="kk,uz",
            tts_engine="piper",
            tts_language="kk",
        ),
        timings={"stt_ms": 10.0, "translation_ms": 2.0, "tts_ms": 5.0, "total_ms": 17.0},
        transcript="Привет",
        stt_status={"status": "loaded"},
        translations={"kk": "Сәлем", "uz": "Salom"},
        translation_status={"engines": {"tilmash": {"status": "loaded"}}},
        fallback_used=False,
        tts_result=TTSResult(b"wav", "audio/wav", "kk", "piper-kk", "local", None, 5, False, 5, {"engine": "piper"}),
        tts_cache_test=None,
        errors=[],
    )

    assert report["verdict"] == "PASS"
    assert report["translation"]["fallback_used"] is False
    assert report["tts"]["bytes"] == 3


def test_aggregate_report_marks_degraded_when_mock_fallback_used():
    report = aggregate_report(
        options=SimpleNamespace(
            audio_wav="sample.wav",
            stt_provider="faster_whisper",
            translation_provider="local",
            translation_fallback="mock",
            target_languages="kk",
            tts_engine="piper",
            tts_language="kk",
        ),
        timings={"stt_ms": 10.0, "translation_ms": 2.0, "tts_ms": 5.0, "total_ms": 17.0},
        transcript="Привет",
        stt_status={"status": "loaded"},
        translations={"kk": "[kk mock] Привет"},
        translation_status={"engines": {"tilmash": {"status": "not_configured"}}},
        fallback_used=True,
        tts_result=TTSResult(b"wav", "audio/wav", "kk", "piper-kk", "local", None, 5, False, 5, {}),
        tts_cache_test={"first_cached": False, "second_cached": True},
        errors=[],
    )

    assert report["verdict"] == "DEGRADED"
    assert report["translation"]["fallback_used"] is True
    assert report["translation"]["tilmash_status"] == "not_configured"


def test_missing_tilmash_status_does_not_fail_report():
    status = local_translation_status_summary({"engines": {"tilmash": {"status": "not_configured", "missing": ["TILMASH_MODEL_PATH"]}}})

    assert status["tilmash_status"] == "not_configured"
    assert status["tilmash_missing"] == ["TILMASH_MODEL_PATH"]


def test_build_verdict_fail_when_required_stage_errors():
    assert build_verdict(errors=["stt failed"], fallback_used=False) == "FAIL"


def test_sanitize_report_redacts_tokens_and_authorization_values():
    report = {
        "error": "bad hf_abc123 token",
        "nested": {"Authorization": "Bearer hf_secret", "url": "https://x.test?a=1&token=hf_secret"},
    }

    sanitized = sanitize_report(report)

    assert "hf_" not in str(sanitized)
    assert "Bearer" not in str(sanitized)
