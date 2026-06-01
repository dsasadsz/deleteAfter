from datetime import datetime, timezone
from time import perf_counter
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.db.repositories import LessonRepository
from app.security.schemas import TokenErrorCode
from app.security.scopes import CAPTIONS_READ, TTS_PLAY
from app.security.rate_limit import check_rate_limit, rate_limit_http_exception, rate_limit_key, subject_for_request
from app.security.tokens import TokenError, create_access_token, require_lesson, require_scope, verify_access_token
from app.providers.quotas import classify_provider_error, record_provider_error
from app.tts.base import SUPPORTED_TTS_LANGUAGES, TTSConfigurationError, TTSSynthesisError, normalize_tts_language
from app.tts.cache import synthesize_with_cache
from app.tts.factory import create_tts_provider
from app.tts.local_tts import local_tts_provider_kwargs
from app.tts.schemas import TTSAudioURLResponse, TTSSynthesizeRequest, TTSStatusResponse
from app.tts.shared_cache import TTSSharedCacheResult, audio_id_for_cache_key, build_tts_shared_cache_key, get_or_synthesize_with_distributed_lock, is_unplayable_tts_text
from app.tts.voice_catalog import split_voice_ids

router = APIRouter(tags=["tts"])


def get_db(request: Request):
    yield from request.app.state.database.session()


@router.get("/api/tts/status", response_model=TTSStatusResponse, response_model_exclude_none=True)
def tts_status(request: Request) -> TTSStatusResponse:
    settings = request.app.state.settings
    supported_languages = sorted(SUPPORTED_TTS_LANGUAGES)
    providers = _provider_statuses(settings)
    active_status = providers.get(settings.tts_provider, _not_ready_status(f"Unknown TTS provider: {settings.tts_provider}"))
    if not settings.tts_enabled:
        return TTSStatusResponse(
            enabled=False,
            provider=settings.tts_provider,
            active_provider=settings.tts_provider,
            ready=False,
            missing=[],
            voices=active_status["voices"],
            default_voice_by_language=active_status["default_voice_by_language"],
            providers=providers,
            selected_voice_support=_selected_voice_support(settings, providers=providers),
            supported_languages=supported_languages,
            shared_cache_enabled=_shared_cache_enabled(settings),
            audio_url_enabled=bool(getattr(settings, "tts_audio_url_enabled", True)),
        )
    return TTSStatusResponse(
        enabled=True,
        provider=settings.tts_provider,
        active_provider=settings.tts_provider,
        ready=bool(active_status.get("ready", True)),
        missing=list(active_status.get("missing", [])),
        voices=dict(active_status.get("voices", {})),
        default_voice_by_language=dict(active_status.get("default_voice_by_language", {})),
        providers=providers,
        selected_voice_support=_selected_voice_support(settings, providers=providers),
        supported_languages=supported_languages,
        shared_cache_enabled=_shared_cache_enabled(settings),
        audio_url_enabled=bool(getattr(settings, "tts_audio_url_enabled", True)),
    )


@router.post("/api/lessons/{lesson_id}/tts/synthesize")
async def synthesize_tts(
    lesson_id: str,
    payload: TTSSynthesizeRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    _authorize_tts_http(request, lesson_id)
    return await synthesize_tts_for_lesson(lesson_id, payload, request, db)


async def synthesize_tts_for_lesson(
    lesson_id: str,
    payload: TTSSynthesizeRequest,
    request: Request,
    db: Session,
) -> Response:
    started_at = perf_counter()
    settings = request.app.state.settings
    await _enforce_tts_rate_limit(request, lesson_id)
    if not settings.tts_enabled:
        raise HTTPException(status_code=503, detail="TTS is disabled")
    if LessonRepository(db).get(lesson_id) is None:
        raise HTTPException(status_code=404, detail="Lesson not found")

    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="TTS text is required")
    if is_unplayable_tts_text(text):
        raise HTTPException(status_code=400, detail="TTS text is unavailable or waiting")
    if len(text) > settings.tts_max_text_chars:
        raise HTTPException(status_code=400, detail="TTS text exceeds maximum length")
    payload.language = normalize_tts_language(payload.language)
    if payload.language not in SUPPORTED_TTS_LANGUAGES:
        raise HTTPException(status_code=400, detail="Unsupported TTS language")
    if payload.return_mode == "url" and not bool(getattr(settings, "tts_audio_url_enabled", True)):
        raise HTTPException(status_code=400, detail="TTS audio URL mode is disabled")
    if payload.return_mode == "url" and not _shared_cache_enabled(settings):
        raise HTTPException(status_code=400, detail="TTS shared cache is required for audio URL mode")

    cache = request.app.state.tts_cache if settings.tts_cache_enabled else None
    provider_name = payload.provider or settings.tts_provider
    try:
        provider = _create_provider(settings, provider_name)
        voice = _validated_voice_or_response(provider, payload.language, payload.voice, payload.voice_gender)
        if isinstance(voice, JSONResponse):
            return voice
        shared = await _synthesize_with_shared_cache(
            lesson_id=lesson_id,
            payload=payload,
            request=request,
            provider=provider,
            provider_name=provider_name,
            text=text,
            voice=voice,
            legacy_cache=cache,
        )
        result = shared.result
    except TTSConfigurationError as exc:
        record_provider_error(request.app, exc)
        _record_runtime_provider_error(request, provider_name, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TTSSynthesisError as exc:
        record_provider_error(request.app, exc)
        _record_runtime_provider_error(request, provider_name, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    _record_runtime_tts_request(request, started_at)

    if payload.return_mode == "url":
        audio_url = _tts_audio_url_path(request, lesson_id, shared.audio_id)
        return JSONResponse(
            content=TTSAudioURLResponse(
                audio_url=audio_url,
                cached=result.cached,
                provider=result.provider,
                voice=result.voice,
                language=result.language,
                caption_id=payload.caption_id,
                expires_at=shared.expires_at.isoformat(),
                audio_mime_type=result.content_type,
            ).model_dump()
        )

    cache_status = "hit" if result.cached else "miss"
    return Response(
        content=result.audio_bytes,
        media_type=result.content_type,
        headers={
            "X-TTS-Cache": cache_status,
            "X-TTS-Cache-Key": _safe_cache_key(shared.cache_key),
            "X-TTS-Provider": result.provider,
            "X-TTS-Language": result.language,
            "X-TTS-Cached": str(result.cached).lower(),
            "X-TTS-Voice": _safe_header_value(result.voice or ""),
            "X-TTS-Latency-Ms": str(result.latency_ms),
        },
    )


@router.get("/api/lessons/{lesson_id}/tts/audio/{audio_id}")
async def get_tts_audio(lesson_id: str, audio_id: str, request: Request, *, already_authorized: bool = False) -> Response:
    if not already_authorized:
        _authorize_tts_http(request, lesson_id, allow_captions_read=False)
    cache = getattr(request.app.state, "tts_shared_cache", None)
    cached = cache.get_audio(audio_id, lesson_id) if cache is not None else None
    if cached is None:
        raise HTTPException(status_code=404, detail="TTS audio not found or expired")
    result = cached.result
    return Response(
        content=result.audio_bytes,
        media_type=result.content_type,
        headers={
            "X-TTS-Cache": "hit",
            "X-TTS-Cache-Key": _safe_cache_key(cached.cache_key),
            "X-TTS-Provider": result.provider,
            "X-TTS-Language": result.language,
            "X-TTS-Cached": "true",
            "X-TTS-Voice": _safe_header_value(result.voice or ""),
            "X-TTS-Expires-At": cached.expires_at.isoformat(),
        },
    )


async def _synthesize_with_shared_cache(
    *,
    lesson_id: str,
    payload: TTSSynthesizeRequest,
    request: Request,
    provider,
    provider_name: str,
    text: str,
    voice: str | None,
    legacy_cache,
):
    settings = request.app.state.settings
    metadata = {"lesson_id": lesson_id, "caption_id": payload.caption_id, "sequence": payload.sequence}
    shared_cache = getattr(request.app.state, "tts_shared_cache", None)
    shared_enabled = _shared_cache_enabled(settings) and shared_cache is not None
    cache_voice = _cache_voice_component(provider, payload.language, voice, payload.voice_gender)
    cache_key = build_tts_shared_cache_key(
        lesson_id=lesson_id,
        caption_id=payload.caption_id,
        language=payload.language,
        provider=provider_name,
        voice=cache_voice,
        text=text,
    )
    if shared_enabled:
        return await get_or_synthesize_with_distributed_lock(
            shared_cache,
            cache_key,
            lambda: provider.synthesize(
                text,
                payload.language,
                voice,
                settings.tts_audio_format,
                metadata,
                voice_gender=payload.voice_gender,
            ),
            settings=settings,
            redis_client=getattr(request.app.state, "redis", None),
            runtime_metrics=getattr(request.app.state, "runtime_metrics", None),
            lesson_id=lesson_id,
        )

    result = await synthesize_with_cache(
        legacy_cache,
        provider,
        text,
        payload.language,
        voice,
        settings.tts_audio_format,
        metadata=metadata,
        voice_gender=payload.voice_gender,
    )
    return TTSSharedCacheResult(
        cache_key=cache_key,
        audio_id=audio_id_for_cache_key(cache_key),
        result=result,
        expires_at=datetime.now(timezone.utc),
    )


def _cache_voice_component(provider, language: str, voice: str | None, voice_gender: str | None) -> str | None:
    value = voice or (f"gender:{voice_gender}" if voice_gender and voice_gender != "auto" else None)
    if getattr(provider, "name", None) != "local" or not hasattr(provider, "_engine_for_language"):
        return value
    try:
        engine_name, _engine = provider._engine_for_language(language)
    except Exception:
        return value
    return f"{engine_name}:{value or 'default'}"


def _shared_cache_enabled(settings) -> bool:
    backend = str(getattr(settings, "tts_shared_cache_backend", "memory") or "memory").lower()
    return bool(getattr(settings, "tts_shared_cache_enabled", True) and backend in {"memory", "disk"})


def _tts_audio_url_path(request: Request, lesson_id: str, audio_id: str) -> str:
    if request.url.path.startswith("/api/v1/integration/"):
        path = f"/api/v1/integration/lessons/{lesson_id}/tts/audio/{audio_id}"
    else:
        path = f"/api/lessons/{lesson_id}/tts/audio/{audio_id}"
    token = _tts_audio_token(request, lesson_id)
    if token:
        return f"{path}?token={quote(token, safe='')}"
    return path


def _tts_audio_token(request: Request, lesson_id: str) -> str | None:
    settings = request.app.state.settings
    if not bool(getattr(settings, "tts_audio_url_token_required", True)):
        return None
    if not settings.security_signing_secret:
        if not settings.websocket_auth_required and not settings.is_production and settings.allow_dev_ws_without_token:
            return None
        raise HTTPException(status_code=503, detail="TTS audio URL signing is not configured")
    return create_access_token(
        {
            "sub": "tts-audio",
            "role": "student",
            "lesson_id": lesson_id,
            "scopes": [TTS_PLAY],
        },
        ttl_seconds=max(1, int(getattr(settings, "tts_audio_url_ttl_seconds", 3600) or 3600)),
    )


def _create_provider(settings, provider_name: str | None = None):
    name = provider_name or settings.tts_provider
    if name == "azure":
        return create_tts_provider(
            name,
            api_key=settings.azure_tts_key,
            region=settings.azure_tts_region,
            endpoint=settings.azure_tts_endpoint,
            voices=_azure_default_voices(settings),
            voice_lists=_azure_voice_lists(settings),
            gender_voices=_azure_gender_voices(settings),
        )
    if name == "elevenlabs":
        return create_tts_provider(
            name,
            api_key=settings.elevenlabs_api_key,
            voices=_elevenlabs_voice_lists(settings),
        )
    if name == "local":
        return create_tts_provider(name, **local_tts_provider_kwargs(settings))
    return create_tts_provider(name)


def _provider_statuses(settings) -> dict:
    statuses = {}
    for provider_name in ("azure", "elevenlabs", "mock", "local"):
        try:
            provider = _create_provider(settings, provider_name)
            status = provider.status() if hasattr(provider, "status") else {"ready": True, "status": "ready", "missing": [], "voices": {}, "default_voice_by_language": {}}
            statuses[provider_name] = _normalize_provider_status(provider_name, status)
        except TTSConfigurationError as exc:
            statuses[provider_name] = _not_ready_status(str(exc), experimental=provider_name == "elevenlabs")
    return statuses


def _normalize_provider_status(provider_name: str, status: dict) -> dict:
    voices = dict(status.get("voices", {}))
    for language in SUPPORTED_TTS_LANGUAGES:
        voices.setdefault(language, [])
    ready = bool(status.get("ready", True))
    return {
        "ready": ready,
        "status": status.get("status") or ("ready" if ready else "not_configured"),
        "supported_languages": sorted(SUPPORTED_TTS_LANGUAGES),
        "voices": voices,
        "default_voice_by_language": dict(status.get("default_voice_by_language", {})),
        "missing": list(status.get("missing", [])),
        "experimental": bool(status.get("experimental", provider_name == "elevenlabs")),
        "engines": dict(status.get("engines", {})),
        "selected_engine_by_language": dict(status.get("selected_engine_by_language", {})),
        "language_status": dict(status.get("language_status", {})),
        "allowed_languages": list(status.get("allowed_languages", sorted(SUPPORTED_TTS_LANGUAGES))),
    }


def _not_ready_status(message: str, experimental: bool = False) -> dict:
    return {
        "ready": False,
        "status": "not_configured",
        "supported_languages": sorted(SUPPORTED_TTS_LANGUAGES),
        "voices": {},
        "default_voice_by_language": {},
        "missing": [message],
        "experimental": experimental,
        "engines": {},
        "selected_engine_by_language": {},
    }


def _validated_voice_or_response(provider, language: str, requested_voice: str | None, voice_gender: str | None):
    status = provider.status() if hasattr(provider, "status") else {}
    language_state = status.get("language_status", {}).get(language, {})
    if language_state.get("status") == "disabled":
        raise TTSConfigurationError(f"{provider.name} TTS is disabled for {language}")
    voices = list(status.get("voices", {}).get(language, []))
    default_voice = status.get("default_voice_by_language", {}).get(language, "")
    if requested_voice and not voices:
        return _voice_not_available_response(provider.name, language, requested_voice)
    selected_voice = requested_voice or default_voice or (voices[0]["id"] if voices else "")
    if not selected_voice:
        return _voice_not_available_response(provider.name, language, requested_voice)
    if voices and selected_voice not in {voice["id"] for voice in voices}:
        return _voice_not_available_response(provider.name, language, selected_voice)
    return selected_voice


def _voice_not_available_response(provider: str, language: str, voice: str | None) -> JSONResponse:
    message = "Voice is not available for selected provider and language."
    return JSONResponse(
        status_code=400,
        content={
            "detail": message,
            "error": {
                "code": "VOICE_NOT_AVAILABLE_FOR_LANGUAGE",
                "message": message,
                "details": {"provider": provider, "language": language, "voice": voice},
            },
        },
    )


def _selected_voice_support(settings, providers: dict | None = None) -> dict:
    provider_support = {
        name: {"status": status.get("status"), "ready": status.get("ready"), "experimental": status.get("experimental", False)}
        for name, status in (providers or {}).items()
    }
    return {
        "provider_override": True,
        "voice": True,
        "voice_gender": True,
        "providers": provider_support,
    }


def _azure_default_voices(settings) -> dict[str, str]:
    return {
        "kk": settings.azure_tts_default_voice_kk,
        "uz": settings.azure_tts_default_voice_uz,
        "zh-Hans": settings.azure_tts_default_voice_zh,
        "ru": settings.azure_tts_default_voice_ru,
    }


def _azure_voice_lists(settings) -> dict[str, list[str]]:
    return {
        "kk": split_voice_ids(settings.azure_tts_voices_kk),
        "uz": split_voice_ids(settings.azure_tts_voices_uz),
        "zh-Hans": split_voice_ids(settings.azure_tts_voices_zh),
        "ru": split_voice_ids(settings.azure_tts_voices_ru),
    }


def _azure_gender_voices(settings) -> dict[str, dict[str, str]]:
    return {
        "kk": {"male": settings.azure_tts_voice_kk_male, "female": settings.azure_tts_voice_kk_female},
        "uz": {"male": settings.azure_tts_voice_uz_male, "female": settings.azure_tts_voice_uz_female},
        "zh-Hans": {"male": settings.azure_tts_voice_zh_male, "female": settings.azure_tts_voice_zh_female},
        "ru": {"male": settings.azure_tts_voice_ru_male, "female": settings.azure_tts_voice_ru_female},
    }


def _elevenlabs_voice_lists(settings) -> dict[str, list[str]]:
    return {
        "kk": split_voice_ids(settings.elevenlabs_tts_voices_kk) or _legacy_elevenlabs_voice_ids(settings.elevenlabs_tts_voice_id_kk_male, settings.elevenlabs_tts_voice_id_kk_female),
        "uz": split_voice_ids(settings.elevenlabs_tts_voices_uz) or _legacy_elevenlabs_voice_ids(settings.elevenlabs_tts_voice_id_uz_male, settings.elevenlabs_tts_voice_id_uz_female),
        "zh-Hans": split_voice_ids(settings.elevenlabs_tts_voices_zh) or _legacy_elevenlabs_voice_ids(settings.elevenlabs_tts_voice_id_zh_male, settings.elevenlabs_tts_voice_id_zh_female),
        "ru": split_voice_ids(settings.elevenlabs_tts_voices_ru) or _legacy_elevenlabs_voice_ids(settings.elevenlabs_tts_voice_id_ru_male, settings.elevenlabs_tts_voice_id_ru_female),
    }


def _legacy_elevenlabs_voice_ids(*voice_ids: str) -> list[str]:
    return [voice_id for voice_id in voice_ids if voice_id]


def _authorize_tts_http(request: Request, lesson_id: str, *, allow_captions_read: bool = True) -> None:
    settings = request.app.state.settings
    token = request.query_params.get("token") or _bearer_token(request.headers.get("authorization"))
    if not token and not settings.websocket_auth_required and not settings.is_production and settings.allow_dev_ws_without_token:
        return
    try:
        payload = verify_access_token(token)
        require_lesson(payload, lesson_id)
        if TTS_PLAY not in payload.scopes:
            require_scope(payload, CAPTIONS_READ if allow_captions_read else TTS_PLAY)
    except TokenError as exc:
        status_code = 403 if exc.code in {TokenErrorCode.TOKEN_SCOPE_MISSING, TokenErrorCode.TOKEN_LESSON_MISMATCH} else 401
        raise HTTPException(status_code=status_code, detail="Missing or invalid TTS access token.") from exc


async def _enforce_tts_rate_limit(request: Request, lesson_id: str) -> None:
    settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return
    if _tts_load_test_rate_limit_bypass_allowed(request):
        return
    subject = subject_for_request(request, lesson_id)
    key = rate_limit_key("tts", lesson_id, subject)
    result = await check_rate_limit(request.app.state.rate_limiter, key, settings.tts_rate_limit_per_minute)
    if not result.allowed:
        record_provider_error(request.app, "TTS rate limit 429")
        _record_runtime_provider_error(request, getattr(request.app.state.settings, "tts_provider", None), "TTS rate limit 429")
        raise rate_limit_http_exception("TTS_RATE_LIMITED", result)


def _tts_load_test_rate_limit_bypass_allowed(request: Request) -> bool:
    settings = request.app.state.settings
    requested = request.headers.get("x-tts-load-test-bypass-rate-limit", "").strip().lower()
    if requested not in {"1", "true", "yes", "on"}:
        return False
    return bool(
        getattr(settings, "tts_load_test_bypass_rate_limit", False)
        and getattr(settings, "enable_load_test_endpoints", False)
        and getattr(settings, "app_env", "").lower() == "development"
    )


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "Bearer "
    if value.startswith(prefix):
        return value[len(prefix) :]
    return None


def _safe_header_value(value: str) -> str:
    safe = []
    for character in value:
        if ord(character) < 32 or ord(character) == 127:
            break
        safe.append(character)
    return "".join(safe)


def _safe_cache_key(cache_key: str) -> str:
    return audio_id_for_cache_key(cache_key)


def _record_runtime_tts_request(request: Request, started_at: float) -> None:
    metrics = getattr(request.app.state, "runtime_metrics", None)
    if metrics is None:
        return
    latency_ms = int((perf_counter() - started_at) * 1000)
    metrics.record_tts_request(latency_ms)


def _record_runtime_provider_error(request: Request, provider: str | None = None, error: Exception | str | None = None) -> None:
    metrics = getattr(request.app.state, "runtime_metrics", None)
    if metrics is not None:
        metrics.record_provider_error(provider, error)
