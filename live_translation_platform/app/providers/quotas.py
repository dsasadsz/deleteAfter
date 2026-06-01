from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.db.models import UsageRecord


RECOMMENDATION_UNKNOWN = "unknown"
RECOMMENDATION_OK = "ok"
RECOMMENDATION_NEAR_LIMIT = "near_limit"
RECOMMENDATION_OVER_LIMIT = "over_limit"


def provider_quota_snapshot(settings: Any, provider_type: str, provider_name: str) -> dict:
    provider_type = provider_type.lower()
    provider_name = provider_name.lower()
    stt_limit = None
    tts_rps_limit = None
    tts_concurrent_limit = None
    translator_rps_limit = None

    if provider_type == "stt":
        stt_limit = _quota_int(settings, f"{provider_name}_stt_max_concurrent_sessions")
    elif provider_type == "translation":
        translator_rps_limit = _quota_int(settings, f"{provider_name}_translator_max_requests_per_second")
    elif provider_type == "tts":
        tts_rps_limit = _quota_int(settings, f"{provider_name}_tts_max_requests_per_second")
        tts_concurrent_limit = _quota_int(settings, f"{provider_name}_tts_max_concurrent_requests")

    source = "config" if any(value is not None for value in (stt_limit, tts_rps_limit, tts_concurrent_limit, translator_rps_limit)) else "unknown"
    return {
        "stt_concurrent_limit": stt_limit,
        "tts_rps_limit": tts_rps_limit,
        "tts_concurrent_limit": tts_concurrent_limit,
        "translator_rps_limit": translator_rps_limit,
        "source": source,
    }


def provider_runtime_snapshot(app: Any | None, provider_type: str, provider_name: str) -> dict:
    runtime = getattr(getattr(app, "state", None), "provider_runtime", {}) if app is not None else {}
    active_lessons = _active_lessons(app)
    return {
        "active_lessons": active_lessons,
        "active_stt_streams": int(runtime.get("active_stt_streams", active_lessons if provider_type == "stt" else 0) or 0),
        "tts_requests_last_minute": int(runtime.get("tts_requests_last_minute", _usage_count_last_minute(app, "tts", provider_name)) or 0),
        "translation_requests_last_minute": int(runtime.get("translation_requests_last_minute", _usage_count_last_minute(app, "translation", provider_name)) or 0),
        "provider_429_count": int(runtime.get("provider_429_count", 0) or 0),
        "last_rate_limit_error": sanitize_provider_error(runtime.get("last_rate_limit_error"), getattr(getattr(app, "state", None), "settings", None)),
    }


def enrich_provider_status(status: dict, settings: Any, app: Any | None = None) -> dict:
    enriched = dict(status)
    for provider_type, providers in list(enriched.items()):
        if provider_type not in {"stt", "translation", "tts"} or not isinstance(providers, dict):
            continue
        for provider_name, payload in list(providers.items()):
            if not isinstance(payload, dict):
                continue
            quotas = provider_quota_snapshot(settings, provider_type, provider_name)
            runtime = provider_runtime_snapshot(app, provider_type, provider_name)
            recommendation = provider_recommendation(provider_type, quotas, runtime)
            payload["quotas"] = quotas
            payload["runtime"] = runtime
            payload["recommendation"] = recommendation
            payload["recommended_action"] = recommended_action(recommendation)
            payload.setdefault("current_errors", {})
            if runtime["last_rate_limit_error"]:
                payload["current_errors"]["last_rate_limit_error"] = runtime["last_rate_limit_error"]
    return enriched


def provider_quota_warnings(status: dict) -> list[str]:
    warnings: list[str] = []
    for provider_type in ("stt", "translation", "tts"):
        providers = status.get(provider_type, {})
        if not isinstance(providers, dict):
            continue
        for provider_name, payload in providers.items():
            recommendation = payload.get("recommendation") if isinstance(payload, dict) else None
            if recommendation == RECOMMENDATION_NEAR_LIMIT:
                warnings.append(f"PROVIDER_QUOTA_NEAR_LIMIT:{provider_type}.{provider_name}")
            elif recommendation == RECOMMENDATION_OVER_LIMIT:
                warnings.append(f"PROVIDER_QUOTA_OVER_LIMIT:{provider_type}.{provider_name}")
    return warnings


def provider_quota_checks(status: dict) -> dict:
    return {
        provider_type: {
            provider_name: {
                "quotas": payload.get("quotas", {}),
                "runtime": payload.get("runtime", {}),
                "recommendation": payload.get("recommendation", RECOMMENDATION_UNKNOWN),
            }
            for provider_name, payload in providers.items()
            if isinstance(payload, dict)
        }
        for provider_type, providers in status.items()
        if provider_type in {"stt", "translation", "tts"} and isinstance(providers, dict)
    }


def provider_recommendation(provider_type: str, quotas: dict, runtime: dict) -> str:
    if runtime.get("provider_429_count", 0) > 0:
        return RECOMMENDATION_OVER_LIMIT
    ratios = []
    if provider_type == "stt":
        ratios.append(_ratio(runtime.get("active_stt_streams"), quotas.get("stt_concurrent_limit")))
    elif provider_type == "translation":
        ratios.append(_ratio(runtime.get("translation_requests_last_minute"), _per_minute(quotas.get("translator_rps_limit"))))
    elif provider_type == "tts":
        ratios.append(_ratio(runtime.get("tts_requests_last_minute"), _per_minute(quotas.get("tts_rps_limit"))))
    ratios = [value for value in ratios if value is not None]
    if not ratios:
        return RECOMMENDATION_UNKNOWN
    if any(value >= 1.0 for value in ratios):
        return RECOMMENDATION_OVER_LIMIT
    if any(value >= 0.8 for value in ratios):
        return RECOMMENDATION_NEAR_LIMIT
    return RECOMMENDATION_OK


def recommended_action(recommendation: str) -> str:
    return {
        RECOMMENDATION_OK: "ok",
        RECOMMENDATION_NEAR_LIMIT: "watch capacity or reduce concurrent load",
        RECOMMENDATION_OVER_LIMIT: "reduce load, request quota increase, or switch provider",
        RECOMMENDATION_UNKNOWN: "configure provider quota hints",
    }.get(recommendation, "configure provider quota hints")


def classify_provider_error(error: Exception | str | None) -> str | None:
    if error is None:
        return None
    text = str(error).lower()
    if "429" in text or "rate limit" in text or "rate_limited" in text or "too many requests" in text:
        return "rate_limit"
    if "quota" in text or "insufficient" in text or "limit exceeded" in text:
        return "quota"
    if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text or "invalid key" in text or "auth" in text:
        return "auth"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "disconnect" in text or "disconnected" in text or "connection closed" in text or "websocket closed" in text:
        return "disconnected"
    return "unknown"


def record_provider_error(app: Any, error: Exception | str | None) -> None:
    if app is None or error is None:
        return
    runtime = getattr(app.state, "provider_runtime", None)
    if runtime is None:
        runtime = {}
        app.state.provider_runtime = runtime
    classification = classify_provider_error(error)
    if classification == "rate_limit":
        runtime["provider_429_count"] = int(runtime.get("provider_429_count", 0) or 0) + 1
        runtime["last_rate_limit_error"] = str(error)


def sanitize_provider_error(error: Any, settings: Any | None = None) -> str | None:
    if error is None:
        return None
    text = str(error)
    for value in _secret_values(settings):
        if value:
            text = text.replace(value, "[redacted]")
    return text


def _quota_int(settings: Any, field_name: str) -> int | None:
    value = getattr(settings, field_name, None)
    if value in {None, ""}:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _active_lessons(app: Any | None) -> int:
    session_manager = getattr(getattr(app, "state", None), "session_manager", None)
    return len(getattr(session_manager, "sessions", {}) or {})


def _usage_count_last_minute(app: Any | None, provider_type: str, provider_name: str) -> int:
    database = getattr(getattr(app, "state", None), "database", None)
    if database is None:
        return 0
    start = datetime.utcnow() - timedelta(seconds=60)
    try:
        with database.session_factory() as session:
            value = session.scalar(
                select(func.count())
                .select_from(UsageRecord)
                .where(
                    UsageRecord.provider_type == provider_type,
                    UsageRecord.provider_name == provider_name,
                    UsageRecord.created_at >= start,
                )
            )
            return int(value or 0)
    except Exception:
        return 0


def _ratio(value: int | float | None, limit: int | float | None) -> float | None:
    if value is None or not limit:
        return None
    return float(value) / float(limit)


def _per_minute(rps: int | None) -> int | None:
    return rps * 60 if rps is not None else None


def _secret_values(settings: Any | None) -> list[str]:
    if settings is None:
        return []
    names = (
        "elevenlabs_api_key",
        "azure_speech_key",
        "azure_translator_key",
        "azure_tts_key",
        "cartesia_api_key",
        "openai_api_key",
    )
    return [str(getattr(settings, name, "")) for name in names if getattr(settings, name, "")]
