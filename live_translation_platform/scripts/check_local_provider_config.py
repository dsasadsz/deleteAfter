from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import Settings, get_settings
from app.tts.base import SUPPORTED_TTS_LANGUAGES, normalize_tts_language
from app.translation.local_engines.base import normalize_translation_language


def build_config_report(settings: Settings, *, verbose: bool = False) -> dict:
    translation = _translation_report(settings, verbose=verbose)
    tts = _tts_report(settings, verbose=verbose)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "translation": translation,
        "tts": tts,
        "overall_status": _worst_status(
            [
                translation["status"],
                tts["status"],
                *[item["status"] for item in translation["routes"].values()],
                *[item["status"] for item in tts["languages"].values()],
                *[item["status"] for item in translation["engines"].values()],
                *[item["status"] for item in tts["engines"].values()],
            ]
        ),
    }


def print_human_table(report: dict) -> None:
    rows = [
        ("translation", "provider", report["translation"]["provider"], report["translation"]["status"], ", ".join(report["translation"]["missing"])),
        ("tts", "provider", report["tts"]["provider"], report["tts"]["status"], ", ".join(report["tts"]["missing"])),
    ]
    for section in ("translation", "tts"):
        grouped = "routes" if section == "translation" else "languages"
        for name, item in report[section].get(grouped, {}).items():
            rows.append((section, name, item["summary"], item["status"], ", ".join(item["missing"])))
        for name, item in report[section]["engines"].items():
            rows.append((section, name, item["summary"], item["status"], ", ".join(item["missing"])))
    widths = [12, 14, 34, 15, 40]
    header = ("section", "component", "summary", "status", "missing")
    print(_row(header, widths))
    print(_row(tuple("-" * width for width in widths), widths))
    for row in rows:
        print(_row(row, widths))


def _translation_report(settings: Settings, *, verbose: bool) -> dict:
    engines = {
        "tilmash": _tilmash_report(settings, verbose=verbose),
        "madlad400": _madlad_report(settings, verbose=verbose),
        "m2m100_ct2": _m2m100_report(settings, verbose=verbose),
        "m2m100_1_2b_ct2": _m2m100_1_2b_report(settings, verbose=verbose),
    }
    routes = _translation_routes_report(settings, engines)
    missing = [] if settings.local_translation_enabled else ["LOCAL_TRANSLATION_ENABLED"]
    status = "ready" if settings.local_translation_enabled else "disabled"
    if settings.local_translation_enabled and not any(item["status"] in {"ready", "degraded"} for item in routes.values()):
        status = "not_configured"
    return {
        "provider": settings.translation_provider,
        "local_enabled": settings.local_translation_enabled,
        "routing_enabled": settings.local_translation_routing_enabled,
        "default_engine": settings.local_translation_default_engine,
        "fallback_engine": settings.local_translation_fallback_engine,
        "timeout_seconds": settings.local_translation_timeout_seconds,
        "status": status,
        "missing": missing,
        "routes": routes,
        "engines": engines,
    }


def _tts_report(settings: Settings, *, verbose: bool) -> dict:
    engines = {
        "piper": _piper_report(settings, verbose=verbose),
        "silero": _silero_report(settings, verbose=verbose),
        "kazakh_tts2": _kazakh_tts2_report(settings, verbose=verbose),
    }
    languages = _tts_languages_report(settings, engines)
    missing = [] if settings.local_tts_enabled else ["LOCAL_TTS_ENABLED"]
    status = "ready" if settings.local_tts_enabled else "disabled"
    if settings.local_tts_enabled and not any(item["status"] in {"ready", "degraded"} for item in languages.values()):
        status = "not_configured"
    return {
        "provider": settings.tts_provider,
        "local_enabled": settings.local_tts_enabled,
        "default_engine": settings.local_tts_default_engine,
        "allowed_languages": sorted(_allowed_tts_languages(settings)),
        "language_engines": {
            "ru": settings.local_tts_ru_engine,
            "kk": settings.local_tts_kk_engine,
            "uz": settings.local_tts_uz_engine,
            "zh-Hans": settings.local_tts_zh_engine,
        },
        "timeout_seconds": settings.local_tts_timeout_seconds,
        "status": status,
        "missing": missing,
        "languages": languages,
        "engines": engines,
    }


def _tilmash_report(settings: Settings, *, verbose: bool) -> dict:
    missing = []
    if settings.tilmash_enabled and not (settings.tilmash_model_path or settings.tilmash_server_url):
        missing.append("TILMASH_MODEL_PATH or TILMASH_SERVER_URL")
    return {
        "status": _engine_status(settings.tilmash_enabled, missing, degraded=settings.tilmash_device == "cpu"),
        "summary": "ru->kk,uz",
        "missing": missing,
        "config": {
            "model_path": _config_value(settings.tilmash_model_path, verbose=verbose),
            "tokenizer_path": _config_value(settings.tilmash_tokenizer_path, verbose=verbose),
            "server_url": _config_value(settings.tilmash_server_url, verbose=verbose),
            "server_timeout_seconds": settings.tilmash_server_timeout_seconds,
            "device": settings.tilmash_device,
            "dtype": settings.tilmash_dtype,
            "timeout_seconds": settings.tilmash_timeout_seconds,
            "load_on_startup": settings.tilmash_load_on_startup,
            "max_batch_size": settings.tilmash_max_batch_size,
        },
    }


def _madlad_report(settings: Settings, *, verbose: bool) -> dict:
    missing = []
    if settings.madlad_enabled and not (settings.madlad_model_path or settings.madlad_server_url):
        missing.append("MADLAD_MODEL_PATH or MADLAD_SERVER_URL")
    return {
        "status": _engine_status(settings.madlad_enabled, missing, degraded=settings.madlad_device == "cpu"),
        "summary": "ru->zh-Hans",
        "missing": missing,
        "config": {
            "model_path": _config_value(settings.madlad_model_path, verbose=verbose),
            "tokenizer_path": _config_value(settings.madlad_tokenizer_path, verbose=verbose),
            "server_url": _config_value(settings.madlad_server_url, verbose=verbose),
            "server_timeout_seconds": settings.madlad_server_timeout_seconds,
            "device": settings.madlad_device,
            "dtype": settings.madlad_dtype,
            "quantization": settings.madlad_quantization,
            "timeout_seconds": settings.madlad_timeout_seconds,
            "load_on_startup": settings.madlad_load_on_startup,
            "max_batch_size": settings.madlad_max_batch_size,
        },
    }


def _m2m100_report(settings: Settings, *, verbose: bool) -> dict:
    missing = []
    if settings.m2m100_ct2_enabled:
        if not settings.m2m100_ct2_model_path:
            missing.append("M2M100_CT2_MODEL_PATH")
        if not settings.m2m100_ct2_tokenizer_path:
            missing.append("M2M100_CT2_TOKENIZER_PATH")
    return {
        "status": _engine_status(settings.m2m100_ct2_enabled, missing, degraded=settings.m2m100_ct2_device == "cpu"),
        "summary": f"ru->{settings.m2m100_ct2_supported_targets}",
        "missing": missing,
        "config": {
            "model_path": _config_value(settings.m2m100_ct2_model_path, verbose=verbose),
            "tokenizer_path": _config_value(settings.m2m100_ct2_tokenizer_path, verbose=verbose),
            "device": settings.m2m100_ct2_device,
            "compute_type": settings.m2m100_ct2_compute_type,
            "timeout_seconds": settings.m2m100_ct2_timeout_seconds,
            "load_on_startup": settings.m2m100_ct2_load_on_startup,
            "model_size": settings.m2m100_ct2_default_size,
            "supported_targets": settings.m2m100_ct2_supported_targets,
        },
    }


def _m2m100_1_2b_report(settings: Settings, *, verbose: bool) -> dict:
    missing = []
    if settings.m2m100_1_2b_ct2_enabled:
        if not settings.m2m100_1_2b_ct2_model_path:
            missing.append("M2M100_1_2B_CT2_MODEL_PATH")
        if not settings.m2m100_1_2b_ct2_tokenizer_path:
            missing.append("M2M100_1_2B_CT2_TOKENIZER_PATH")
    return {
        "status": _engine_status(settings.m2m100_1_2b_ct2_enabled, missing, degraded=settings.m2m100_1_2b_ct2_device == "cpu"),
        "summary": f"ru->{settings.m2m100_1_2b_ct2_supported_targets}",
        "missing": missing,
        "config": {
            "model_path": _config_value(settings.m2m100_1_2b_ct2_model_path, verbose=verbose),
            "tokenizer_path": _config_value(settings.m2m100_1_2b_ct2_tokenizer_path, verbose=verbose),
            "device": settings.m2m100_1_2b_ct2_device,
            "compute_type": settings.m2m100_1_2b_ct2_compute_type,
            "timeout_seconds": settings.m2m100_1_2b_ct2_timeout_seconds,
            "load_on_startup": settings.m2m100_1_2b_ct2_load_on_startup,
            "model_size": "1.2b",
            "supported_targets": settings.m2m100_1_2b_ct2_supported_targets,
        },
    }


def _piper_report(settings: Settings, *, verbose: bool) -> dict:
    missing = []
    if settings.piper_enabled and not settings.piper_bin_path:
        missing.append("PIPER_BIN_PATH")
    voices = {
        "ru": settings.piper_voice_ru,
        "kk": settings.piper_voice_kk,
        "uz": settings.piper_voice_uz,
        "zh-Hans": settings.piper_voice_zh,
    }
    missing_voices = [f"PIPER_VOICE_{'ZH' if language == 'zh-Hans' else language.upper()}" for language, value in voices.items() if not value]
    status = _engine_status(settings.piper_enabled, missing, degraded=bool(missing_voices))
    return {
        "status": status,
        "summary": "kk,uz,zh-Hans,ru voices",
        "missing": missing + missing_voices,
        "config": {
            "bin_path": _config_value(settings.piper_bin_path, verbose=verbose),
            "voices": {language: _config_value(value, verbose=verbose) for language, value in voices.items()},
            "output_format": settings.piper_output_format,
            "timeout_seconds": settings.piper_timeout_seconds,
        },
    }


def _silero_report(settings: Settings, *, verbose: bool) -> dict:
    missing = []
    if settings.silero_tts_enabled and not settings.silero_tts_model_path:
        missing.append("SILERO_TTS_MODEL_PATH")
    return {
        "status": _engine_status(settings.silero_tts_enabled, missing, degraded=settings.silero_tts_device == "cpu"),
        "summary": settings.silero_tts_language,
        "missing": missing,
        "config": {
            "model_path": _config_value(settings.silero_tts_model_path, verbose=verbose),
            "device": settings.silero_tts_device,
            "language": settings.silero_tts_language,
            "speaker": settings.silero_tts_speaker,
            "timeout_seconds": settings.silero_tts_timeout_seconds,
        },
    }


def _kazakh_tts2_report(settings: Settings, *, verbose: bool) -> dict:
    missing = []
    if settings.kazakh_tts2_enabled and not settings.kazakh_tts2_server_url:
        if not settings.kazakh_tts2_model_path:
            missing.append("KAZAKH_TTS2_MODEL_PATH")
        if not settings.kazakh_tts2_vocoder_path:
            missing.append("KAZAKH_TTS2_VOCODER_PATH")
        if not settings.kazakh_tts2_tokenizer_path:
            missing.append("KAZAKH_TTS2_TOKENIZER_PATH")
        if missing:
            missing.append("KAZAKH_TTS2_SERVER_URL")
    return {
        "status": _engine_status(settings.kazakh_tts2_enabled, missing, degraded=settings.kazakh_tts2_device == "cpu"),
        "summary": "kk quality",
        "missing": missing,
        "config": {
            "model_path": _config_value(settings.kazakh_tts2_model_path, verbose=verbose),
            "vocoder_path": _config_value(settings.kazakh_tts2_vocoder_path, verbose=verbose),
            "tokenizer_path": _config_value(settings.kazakh_tts2_tokenizer_path, verbose=verbose),
            "server_url": _config_value(settings.kazakh_tts2_server_url, verbose=verbose),
            "server_timeout_seconds": settings.kazakh_tts2_server_timeout_seconds,
            "device": settings.kazakh_tts2_device,
            "dtype": settings.kazakh_tts2_dtype,
            "output_format": settings.kazakh_tts2_output_format,
            "timeout_seconds": settings.kazakh_tts2_timeout_seconds,
            "load_on_startup": settings.kazakh_tts2_load_on_startup,
            "default_voice": settings.kazakh_tts2_default_voice,
        },
    }


def _translation_routes_report(settings: Settings, engines: dict[str, dict]) -> dict:
    configured_routes = {
        "kk": settings.local_translation_route_kk,
        "zh-Hans": settings.local_translation_route_zh,
        "uz": settings.local_translation_route_uz,
    }
    return {language: _translation_route_report(language, route, settings, engines) for language, route in configured_routes.items()}


def _translation_route_report(language: str, route: str, settings: Settings, engines: dict[str, dict]) -> dict:
    target = normalize_translation_language(language)
    engine = (route or _default_translation_route(target, settings)).strip().lower()
    if engine == "disabled":
        return {"status": "disabled", "summary": f"{target}: disabled", "engine": "disabled", "missing": []}
    experimental = _experimental_translation_route_metadata(target, engine)
    engine_status = engines.get(engine)
    if engine_status is None:
        return {
            "status": "not_configured",
            "summary": f"{target}: {engine or 'not configured'}",
            "engine": engine,
            "missing": ["LOCAL_TRANSLATION_ROUTE"],
            **experimental,
        }
    if engine_status["status"] == "disabled":
        return {
            "status": "not_configured",
            "summary": f"{target}: {engine}",
            "engine": engine,
            "missing": [_translation_engine_enabled_env(engine)],
            **experimental,
        }
    status = engine_status["status"]
    if status == "degraded":
        status = "ready"
    if experimental and status in {"ready", "configured", "loaded"}:
        status = "degraded"
        experimental["warning"] = "Uzbek quality failed automatic benchmark and requires manual review."
    return {
        "status": status,
        "summary": f"{target}: {engine}",
        "engine": engine,
        "missing": list(engine_status.get("missing", [])),
        **experimental,
    }


def _default_translation_route(language: str, settings: Settings) -> str:
    if settings.local_translation_routing_enabled and not any([settings.local_translation_route_kk, settings.local_translation_route_zh, settings.local_translation_route_uz]):
        if language in {"kk", "uz"}:
            return "tilmash"
        if language == "zh-Hans":
            return "madlad400"
    return settings.local_translation_default_engine


def _tts_languages_report(settings: Settings, engines: dict[str, dict]) -> dict:
    languages = ("kk", "zh-Hans", "uz", "ru")
    return {language: _tts_language_report(language, settings, engines) for language in languages}


def _tts_language_report(language: str, settings: Settings, engines: dict[str, dict]) -> dict:
    normalized = normalize_tts_language(language)
    engine = _tts_engine_for_language(normalized, settings)
    if normalized not in _allowed_tts_languages(settings) or engine == "disabled":
        return {"status": "disabled", "summary": f"{normalized}: disabled", "engine": "disabled", "missing": []}
    engine_status = engines.get(engine)
    if engine_status is None:
        return {"status": "not_configured", "summary": f"{normalized}: {engine}", "engine": engine, "missing": [f"LOCAL_TTS_ENGINE_{engine.upper()}"]}
    if engine == "piper":
        missing = []
        if settings.piper_enabled and not settings.piper_bin_path:
            missing.append("PIPER_BIN_PATH")
        voice_name = f"PIPER_VOICE_{'ZH' if normalized == 'zh-Hans' else normalized.upper()}"
        voice_value = {
            "kk": settings.piper_voice_kk,
            "zh-Hans": settings.piper_voice_zh,
            "uz": settings.piper_voice_uz,
            "ru": settings.piper_voice_ru,
        }.get(normalized, "")
        if settings.piper_enabled and not voice_value:
            missing.append(voice_name)
    else:
        missing = list(engine_status.get("missing", []))
    status = "not_configured" if missing else ("ready" if engine_status.get("status") in {"ready", "degraded"} else engine_status.get("status", "not_configured"))
    if status == "disabled":
        status = "not_configured"
        missing = [_tts_engine_enabled_env(engine)]
    return {"status": status, "summary": f"{normalized}: {engine}", "engine": engine, "missing": missing}


def _tts_engine_for_language(language: str, settings: Settings) -> str:
    return {
        "kk": settings.local_tts_kk_engine,
        "zh-Hans": settings.local_tts_zh_engine,
        "uz": settings.local_tts_uz_engine,
        "ru": settings.local_tts_ru_engine,
    }.get(language, settings.local_tts_default_engine).strip().lower().replace("-", "_")


def _allowed_tts_languages(settings: Settings) -> set[str]:
    raw = settings.local_tts_allowed_languages
    if not raw:
        return set(SUPPORTED_TTS_LANGUAGES)
    return {normalize_tts_language(item) for item in raw.split(",") if normalize_tts_language(item) in SUPPORTED_TTS_LANGUAGES}


def _translation_engine_enabled_env(engine: str) -> str:
    return {
        "tilmash": "TILMASH_ENABLED",
        "madlad400": "MADLAD_ENABLED",
        "m2m100_ct2": "M2M100_CT2_ENABLED",
        "m2m100_1_2b_ct2": "M2M100_1_2B_CT2_ENABLED",
    }.get(engine, f"LOCAL_TRANSLATION_ENGINE_{engine.upper()}_ENABLED")


def _experimental_translation_route_metadata(language: str, engine: str) -> dict:
    if normalize_translation_language(language) == "uz" and engine == "m2m100_1_2b_ct2":
        return {"experimental": True, "production_ready": False}
    return {}


def _tts_engine_enabled_env(engine: str) -> str:
    return {
        "piper": "PIPER_ENABLED",
        "silero": "SILERO_TTS_ENABLED",
        "kazakh_tts2": "KAZAKH_TTS2_ENABLED",
    }.get(engine, f"LOCAL_TTS_ENGINE_{engine.upper()}_ENABLED")


def _engine_status(enabled: bool, missing: list[str], *, degraded: bool = False) -> str:
    if not enabled:
        return "disabled"
    if missing:
        return "not_configured"
    if degraded:
        return "degraded"
    return "ready"


def _config_value(value: str, *, verbose: bool) -> str:
    if not value:
        return ""
    return value if verbose else "<configured>"


def _worst_status(statuses: list[str]) -> str:
    order = ["error", "not_configured", "degraded", "disabled", "ready"]
    for status in order:
        if status in statuses:
            return status
    return "ready"


def _row(values: tuple[str, ...], widths: list[int]) -> str:
    return "  ".join(str(value)[:width].ljust(width) for value, width in zip(values, widths))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check local translation/TTS provider configuration.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a human-readable table.")
    parser.add_argument("--verbose", action="store_true", help="Show non-secret local paths instead of <configured>.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = build_config_report(get_settings(), verbose=args.verbose)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
