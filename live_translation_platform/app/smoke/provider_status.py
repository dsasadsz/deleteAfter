from app.config import Settings
from app.stt.faster_whisper_stt import faster_whisper_status
from app.translation.base import create_translation_provider
from app.translation.local_provider import local_translation_provider_kwargs
from app.tts.factory import create_tts_provider
from app.tts.local_tts import local_tts_provider_kwargs


def provider_status(settings: Settings) -> dict:
    elevenlabs_missing = []
    if not settings.elevenlabs_api_key:
        elevenlabs_missing.append("ELEVENLABS_API_KEY")

    azure_speech_missing = []
    if not settings.azure_speech_key:
        azure_speech_missing.append("AZURE_SPEECH_KEY")
    if not settings.azure_speech_region:
        azure_speech_missing.append("AZURE_SPEECH_REGION")

    cartesia_missing = []
    if not settings.cartesia_api_key:
        cartesia_missing.append("CARTESIA_API_KEY")

    azure_missing = []
    if not settings.azure_translator_key:
        azure_missing.append("AZURE_TRANSLATOR_KEY")

    local_translation = _local_translation_status(settings)
    local_tts = _local_tts_status(settings)

    return {
        "zoom": zoom_status(settings),
        "browser_audio": {
            "ready": settings.browser_audio_enabled,
            "enabled": settings.browser_audio_enabled,
            "primary": settings.browser_audio_primary,
            "default_audio_source": settings.default_audio_source,
            "websocket_path": "/ws/lessons/{lesson_id}/audio-ingest",
        },
        "stt": {
            "mock": {"ready": True},
            "elevenlabs": elevenlabs_user_probe_status(
                has_api_key=bool(settings.elevenlabs_api_key),
                missing=elevenlabs_missing,
            ),
            "azure": {"ready": not azure_speech_missing, "missing": azure_speech_missing},
            "cartesia": {"ready": not cartesia_missing, "missing": cartesia_missing},
            "faster_whisper": faster_whisper_status(settings),
        },
        "translation": {
            "mock": {"ready": True},
            "azure": {"ready": not azure_missing, "missing": azure_missing},
            "local": local_translation,
        },
        "tts": {
            "mock": {"ready": True, "missing": []},
            "azure": {
                "ready": bool(settings.azure_tts_key and (settings.azure_tts_region or settings.azure_tts_endpoint)),
                "missing": [
                    name
                    for name, value in {
                        "AZURE_TTS_KEY": settings.azure_tts_key,
                        "AZURE_TTS_REGION": settings.azure_tts_region or settings.azure_tts_endpoint,
                    }.items()
                    if not value
                ],
            },
            "elevenlabs": {
                "ready": bool(settings.elevenlabs_api_key),
                "missing": [] if settings.elevenlabs_api_key else ["ELEVENLABS_API_KEY"],
                "experimental": True,
            },
            "local": local_tts,
        },
    }


def _local_translation_status(settings: Settings) -> dict:
    provider = create_translation_provider("local", **local_translation_provider_kwargs(settings))
    return provider.status()


def _local_tts_status(settings: Settings) -> dict:
    provider = create_tts_provider("local", **local_tts_provider_kwargs(settings))
    status = provider.status()
    return status


def elevenlabs_user_probe_status(
    has_api_key: bool,
    status_code: int | None = None,
    payload: dict | None = None,
    missing: list[str] | None = None,
    stt_endpoint_ready: bool | None = None,
) -> dict:
    missing = missing or ([] if has_api_key else ["ELEVENLABS_API_KEY"])
    ready_for_stt = bool(has_api_key) if stt_endpoint_ready is None else bool(stt_endpoint_ready)
    status = {
        "ready": ready_for_stt,
        "ready_for_stt": ready_for_stt,
        "missing": missing,
        "user_endpoint_permission": None,
        "warning": None,
    }
    if not has_api_key:
        return status
    if status_code == 200:
        status["user_endpoint_permission"] = True
        return status
    if status_code == 401 and _elevenlabs_missing_user_read(payload or {}):
        status["ready"] = True
        status["ready_for_stt"] = True
        status["user_endpoint_permission"] = False
        status["warning"] = "API key lacks user_read but STT works"
        return status
    if status_code is not None and status_code >= 400:
        status["user_endpoint_permission"] = False
        status["warning"] = "ElevenLabs user endpoint check failed; STT readiness is independent"
    return status


def _elevenlabs_missing_user_read(payload: dict) -> bool:
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        return False
    message = str(detail.get("message") or "")
    return detail.get("status") == "missing_permissions" and "user_read" in message


def missing_for_selection(settings: Settings, stt_provider: str, translation_provider: str) -> list[str]:
    status = provider_status(settings)
    missing: list[str] = []
    stt = status["stt"].get(stt_provider, {})
    translation = status["translation"].get(translation_provider, {})
    missing.extend(stt.get("missing", []))
    missing.extend(translation.get("missing", []))
    return missing


def zoom_status(settings: Settings) -> dict:
    api_missing = [
        name
        for name, value in {
            "ZOOM_ACCOUNT_ID": settings.zoom_account_id,
            "ZOOM_CLIENT_ID": settings.zoom_client_id,
            "ZOOM_CLIENT_SECRET": settings.zoom_client_secret,
        }.items()
        if not value
    ]
    rtms_missing = [
        name
        for name, value in {
            "ZOOM_RTMS_CLIENT_ID": settings.zoom_rtms_client_id,
            "ZOOM_RTMS_CLIENT_SECRET": settings.zoom_rtms_client_secret,
            "ZOOM_WEBHOOK_SECRET_TOKEN": settings.zoom_webhook_secret_token,
        }.items()
        if not value
    ]
    meeting_sdk_missing = [
        name
        for name, value in {
            "ZOOM_MEETING_SDK_KEY or ZOOM_SDK_KEY": settings.zoom_meeting_sdk_effective_key,
            "ZOOM_MEETING_SDK_SECRET or ZOOM_SDK_SECRET": settings.zoom_meeting_sdk_effective_secret,
        }.items()
        if not value
    ]
    webhook_public_url = settings.zoom_webhook_url or (
        f"{settings.public_base_url.rstrip('/')}{settings.zoom_rtms_webhook_path}" if settings.public_base_url else ""
    )
    status = {
        "api": {"ready": not api_missing, "missing": api_missing},
        "meeting_sdk": {"ready": not meeting_sdk_missing, "missing": meeting_sdk_missing},
        "webhook": {
            "configured": bool(webhook_public_url),
            "public_url": webhook_public_url,
            "path": settings.zoom_rtms_webhook_path,
        },
    }
    if settings.rtms_ui_enabled or settings.rtms_experimental_enabled:
        status["rtms"] = {
            "ready": settings.zoom_rtms_enabled and not rtms_missing,
            "missing": rtms_missing,
            "enabled": settings.zoom_rtms_enabled,
            "experimental_enabled": settings.rtms_experimental_enabled,
            "ui_enabled": settings.rtms_ui_enabled,
        }
    return status
