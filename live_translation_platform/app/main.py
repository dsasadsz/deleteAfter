import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import captions, compare, e2e_qa, exports, glossaries, health, integration, lessons, live_tests, load_test, local_load_tests, monitoring, providers, questions, real_test, smoke, tts, usage, zoom
from app.compare.hub import ComparisonEventHub
from app.compare.runner import ComparisonRunner
from app.config import get_settings, reset_settings_cache
from app.db.database import Database
from app.db.repositories import DebugRepository, MetricsRepository, TranscriptRepository
from app.logging_config import configure_logging
from app.integration.callbacks import IntegrationCallbackSender
from app.infra import redis as redis_infra
from app.infra.pubsub import PubSubStatus, RedisPubSubFanout
from app.live_tests import FinalCaptionCaptureService
from app.loadtest.virtual_lesson_runner import LocalLoadTestRunner
from app.middleware import AccessLogMiddleware, RequestBodySizeLimitMiddleware, RequestIDMiddleware, SecurityHeadersMiddleware
from app.monitoring.metrics import RuntimeMetrics
from app.production import sanitize_for_log
from app.realtime.caption_hub import CaptionHub
from app.realtime.browser_audio_manager import BrowserAudioManager
from app.realtime.lesson_session import LessonSessionManager
from app.stt.faster_whisper_stt import faster_whisper_provider_kwargs
from app.realtime.question_hub import QuestionHub
from app.realtime.rtms_manager import RTMSManager
from app.runtime import BackgroundTaskRegistry, shutdown_runtime
from app.security.rate_limit import InMemoryRateLimiter, RedisRateLimiter
from app.smoke.hub import SmokeEventHub
from app.smoke.runner import SmokeRunner
from app.translation.base import create_translation_provider
from app.translation.local_provider import local_translation_provider_kwargs
from app.tts.cache import TTSCache
from app.tts.factory import create_tts_provider
from app.tts.local_tts import local_tts_provider_kwargs
from app.tts.shared_cache import create_tts_shared_cache
from app.usage.usage_tracker import UsageTracker
from app.questions.service import QuestionService
from app.web import routes as web_routes
from app.zoom.zoom_api_client import ZoomAPIClient
from app.zoom.meeting_sdk import MeetingSDKConfig, ZoomMeetingSDKSignatureService
from app.zoom.zoom_oauth import ZoomOAuthClient


def create_app() -> FastAPI:
    reset_settings_cache()
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Live Translation Platform",
        version="0.1.0",
        description="Mock MVP for live lesson captions and translation.",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIDMiddleware)
    if settings.allowed_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
        )
    if settings.trusted_host_list:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
    app.add_middleware(RequestBodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    _install_exception_handlers(app)

    database = Database(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=settings.database_pool_pre_ping,
        echo=settings.database_echo,
    )
    database.create_all()
    runtime_metrics = RuntimeMetrics()
    hub = CaptionHub(
        runtime_metrics=runtime_metrics,
        send_timeout_seconds=settings.websocket_broadcast_send_timeout_seconds,
        max_concurrency=settings.websocket_broadcast_max_concurrency,
        drop_on_timeout=settings.websocket_broadcast_drop_on_timeout,
        metrics_enabled=settings.websocket_broadcast_metrics_enabled,
    )
    question_hub = QuestionHub(
        runtime_metrics=runtime_metrics,
        send_timeout_seconds=settings.websocket_broadcast_send_timeout_seconds,
        max_concurrency=settings.websocket_broadcast_max_concurrency,
        drop_on_timeout=settings.websocket_broadcast_drop_on_timeout,
        metrics_enabled=settings.websocket_broadcast_metrics_enabled,
    )
    smoke_hub = SmokeEventHub()
    comparison_hub = ComparisonEventHub()
    final_caption_capture = FinalCaptionCaptureService(database.session_factory)
    transcript_repo = TranscriptRepository(database.session_factory, final_caption_capture=final_caption_capture.capture)
    metrics_repo = MetricsRepository(database.session_factory)
    debug_repo = DebugRepository(database.session_factory)
    usage_tracker = UsageTracker(database.session_factory, enabled=settings.usage_tracking_enabled)
    browser_audio_manager = BrowserAudioManager(
        session_factory=database.session_factory,
        hub=hub,
        debug_repo=debug_repo,
        enabled=settings.browser_audio_enabled,
        queue_max_size=settings.browser_audio_queue_max_size,
        drop_policy=settings.browser_audio_drop_policy,
        allow_duplicate_teacher=settings.browser_audio_allow_duplicate_teacher,
        expected_sample_rate=settings.browser_audio_expected_sample_rate,
        expected_channels=settings.browser_audio_expected_channels,
        expected_format=settings.browser_audio_expected_format,
        chunk_ms=settings.browser_audio_chunk_ms,
        use_audio_worklet=settings.browser_audio_use_audio_worklet,
        enable_resample_in_browser=settings.browser_audio_enable_resample_in_browser,
        commit_strategy=settings.elevenlabs_stt_commit_strategy,
        manual_commit_after_silence_ms=settings.elevenlabs_stt_manual_commit_after_silence_ms,
        force_commit_enabled=settings.elevenlabs_stt_force_commit_enabled,
        partials_for_live=settings.elevenlabs_stt_partials_for_live,
        max_segment_duration_ms=settings.browser_audio_max_segment_duration_ms,
        periodic_commit_enabled=settings.browser_audio_periodic_commit_enabled,
    )

    app.state.settings = settings
    app.state.database = database
    app.state.redis = None
    app.state.redis_status = None
    app.state.pubsub = None
    app.state.pubsub_status = PubSubStatus(enabled=settings.redis_pubsub_enabled, connected=False)
    app.state.provider_runtime = {}
    app.state.local_translation_startup_provider = None
    app.state.local_translation_startup_error = None
    app.state.runtime_metrics = runtime_metrics
    app.state.task_registry = BackgroundTaskRegistry()
    app.state.rate_limiter = _create_rate_limiter(settings, None, runtime_metrics)
    app.state.caption_hub = hub
    app.state.question_hub = question_hub
    app.state.smoke_hub = smoke_hub
    app.state.comparison_hub = comparison_hub
    app.state.transcript_repo = transcript_repo
    app.state.final_caption_capture = final_caption_capture
    app.state.metrics_repo = metrics_repo
    app.state.debug_repo = debug_repo
    app.state.usage_tracker = usage_tracker
    app.state.tts_cache = TTSCache(settings.tts_cache_max_items)
    app.state.tts_shared_cache = create_tts_shared_cache(
        settings,
        ttl_seconds=min(settings.tts_shared_cache_ttl_seconds, settings.tts_audio_url_ttl_seconds),
    )
    app.state.browser_audio_manager = browser_audio_manager
    app.state.question_service = QuestionService(database.session_factory, question_hub, settings)
    app.state.integration_callback_sender = IntegrationCallbackSender(
        callback_secret=settings.integration_callback_secret,
    )
    app.state.zoom_oauth_client = ZoomOAuthClient(
        account_id=settings.zoom_account_id,
        client_id=settings.zoom_client_id,
        client_secret=settings.zoom_client_secret,
        token_url=settings.zoom_oauth_token_url,
        timeout_seconds=settings.zoom_http_timeout_seconds,
    )
    app.state.zoom_api_client = ZoomAPIClient(
        oauth_client=app.state.zoom_oauth_client,
        api_base_url=settings.zoom_api_base_url,
        timezone=settings.zoom_default_timezone,
        duration_minutes=settings.zoom_default_duration_minutes,
        timeout_seconds=settings.zoom_http_timeout_seconds,
        max_retries=settings.zoom_http_max_retries,
    )
    app.state.zoom_meeting_sdk = ZoomMeetingSDKSignatureService(
        MeetingSDKConfig(
            client_id=settings.zoom_meeting_sdk_effective_key,
            client_secret=settings.zoom_meeting_sdk_effective_secret,
            leave_url=settings.zoom_meeting_sdk_leave_url,
            lang=settings.zoom_meeting_sdk_lang,
            role_student=settings.zoom_meeting_sdk_role_student,
            role_host=settings.zoom_meeting_sdk_role_host,
        )
    )
    app.state.session_manager = LessonSessionManager(
        hub=hub,
        transcript_repo=transcript_repo,
        metrics_repo=metrics_repo,
        debug_repo=debug_repo,
        session_factory=database.session_factory,
        rtms_manager=None,
        browser_audio_manager=browser_audio_manager,
        source_language=settings.source_language,
        translate_partials=settings.translate_partials,
        rtms_process_audio=settings.rtms_process_audio,
        rtms_experimental_enabled=settings.rtms_experimental_enabled,
        mock_stt_audio_driven=settings.mock_stt_audio_driven,
        mock_stt_chunks_per_partial=settings.mock_stt_chunks_per_partial,
        mock_stt_chunks_per_final=settings.mock_stt_chunks_per_final,
        mock_stt_min_final_interval_ms=settings.mock_stt_min_final_interval_ms,
        audio_pipeline_queue_max_size=settings.audio_pipeline_queue_max_size,
        audio_pipeline_drop_policy=settings.audio_pipeline_drop_policy,
        elevenlabs_stt_config={
            "api_key": settings.elevenlabs_api_key,
            "model_id": settings.elevenlabs_stt_model,
            "language": settings.elevenlabs_stt_language,
            "audio_format": settings.elevenlabs_stt_audio_format,
            "sample_rate": settings.elevenlabs_stt_sample_rate,
            "commit_strategy": settings.elevenlabs_stt_commit_strategy,
            "enable_partials": settings.elevenlabs_stt_partials_for_live and settings.elevenlabs_stt_enable_partials,
            "max_reconnects": settings.elevenlabs_stt_max_reconnects,
            "connect_timeout_seconds": settings.elevenlabs_stt_connect_timeout_seconds,
            "receive_timeout_seconds": settings.elevenlabs_stt_receive_timeout_seconds,
        },
        azure_stt_config={
            "api_key": settings.azure_speech_key,
            "region": settings.azure_speech_region,
            "language": settings.azure_speech_language,
            "sample_rate": settings.azure_speech_sample_rate,
            "bits_per_sample": settings.azure_speech_bits_per_sample,
            "channels": settings.azure_speech_channels,
            "enable_partials": settings.azure_speech_enable_partials,
            "initial_silence_timeout_ms": settings.azure_speech_initial_silence_timeout_ms,
            "segmentation_silence_timeout_ms": settings.azure_speech_segmentation_silence_timeout_ms,
            "profanity": settings.azure_speech_profanity,
            "use_phrase_list": settings.azure_speech_use_phrase_list,
        },
        cartesia_stt_config={
            "api_key": settings.cartesia_api_key,
            "model": settings.cartesia_stt_model,
            "language": settings.cartesia_stt_language,
            "encoding": settings.cartesia_stt_encoding,
            "sample_rate": settings.cartesia_stt_sample_rate,
            "enable_partials": settings.cartesia_stt_enable_partials,
            "max_reconnects": settings.cartesia_stt_max_reconnects,
            "connect_timeout_seconds": settings.cartesia_stt_connect_timeout_seconds,
            "receive_timeout_seconds": settings.cartesia_stt_receive_timeout_seconds,
            "version": settings.cartesia_stt_version,
        },
        faster_whisper_stt_config=faster_whisper_provider_kwargs(settings),
        azure_translator_config={
            "api_key": settings.azure_translator_key,
            "region": settings.azure_translator_region,
            "endpoint": settings.azure_translator_endpoint,
            "api_version": settings.azure_translator_api_version,
        },
        local_translation_config=local_translation_provider_kwargs(settings),
        usage_tracker=usage_tracker,
        runtime_metrics=runtime_metrics,
    )
    app.state.rtms_manager = RTMSManager(
        session_factory=database.session_factory,
        hub=hub,
        debug_repo=debug_repo,
        enabled=settings.zoom_rtms_enabled,
        client_id=settings.zoom_rtms_client_id,
        client_secret=settings.zoom_rtms_client_secret,
        debug_audio_every_n_chunks=settings.rtms_debug_audio_log_every_n_chunks,
        process_audio=settings.rtms_process_audio,
        process_transcript=settings.rtms_process_transcript,
        audio_queue_max_size=settings.rtms_audio_queue_max_size,
        audio_drop_policy=settings.rtms_audio_drop_policy,
    )
    app.state.session_manager.rtms_manager = app.state.rtms_manager
    app.state.smoke_runner = SmokeRunner(
        settings=settings,
        session_factory=database.session_factory,
        smoke_hub=smoke_hub,
        caption_hub=hub,
        usage_tracker=usage_tracker,
    )
    app.state.comparison_runner = ComparisonRunner(
        session_factory=database.session_factory,
        smoke_runner=app.state.smoke_runner,
        comparison_hub=comparison_hub,
    )
    app.state.local_load_test_runner = LocalLoadTestRunner()

    app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
    app.include_router(health.router)
    app.include_router(monitoring.router)
    app.include_router(integration.router)
    app.include_router(lessons.router)
    app.include_router(load_test.router)
    app.include_router(local_load_tests.router)
    app.include_router(e2e_qa.router)
    app.include_router(live_tests.router)
    app.include_router(captions.router)
    app.include_router(questions.router)
    app.include_router(tts.router)
    app.include_router(compare.router)
    app.include_router(exports.router)
    app.include_router(glossaries.router)
    app.include_router(providers.router)
    app.include_router(real_test.router)
    app.include_router(smoke.router)
    app.include_router(usage.router)
    app.include_router(zoom.router)
    app.include_router(web_routes.router)

    @app.on_event("startup")
    async def startup_event() -> None:
        await _initialize_local_translation_if_requested(app, settings)
        await _initialize_local_tts_if_requested(app, settings)
        if not settings.redis_enabled:
            app.state.redis = None
            app.state.redis_status = await redis_infra.redis_client_status(settings, None)
            _disable_pubsub(app)
            app.state.rate_limiter = _create_rate_limiter(settings, None, app.state.runtime_metrics)
            return
        try:
            app.state.redis = await redis_infra.create_redis_client(settings)
        except Exception as exc:
            app.state.redis = None
            app.state.redis_status = redis_infra.redis_error_status(settings, exc)
            _disable_pubsub(app, "Redis client is not initialized.")
            app.state.rate_limiter = _create_rate_limiter(settings, None, app.state.runtime_metrics)
            return
        app.state.rate_limiter = _create_rate_limiter(settings, app.state.redis, app.state.runtime_metrics)
        app.state.redis_status = await redis_infra.redis_client_status(settings, app.state.redis)
        await _start_pubsub(app, settings, app.state.redis)

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        await _stop_pubsub(app)
        await shutdown_runtime(app)

    return app


async def _initialize_local_translation_if_requested(app: FastAPI, settings) -> None:
    if not bool(getattr(settings, "local_translation_enabled", False)):
        return
    if not bool(getattr(settings, "tilmash_load_on_startup", False)):
        return
    provider = create_translation_provider("local", **local_translation_provider_kwargs(settings))
    app.state.local_translation_startup_provider = provider
    try:
        if hasattr(provider, "initialize"):
            await provider.initialize()
    except Exception as exc:
        app.state.local_translation_startup_error = str(exc)


async def _initialize_local_tts_if_requested(app: FastAPI, settings) -> None:
    if not bool(getattr(settings, "local_tts_enabled", False)):
        return
    if not bool(getattr(settings, "kazakh_tts2_load_on_startup", False)):
        return
    provider = create_tts_provider("local", **local_tts_provider_kwargs(settings))
    app.state.local_tts_startup_provider = provider
    try:
        if hasattr(provider, "initialize"):
            await provider.initialize()
    except Exception as exc:
        app.state.local_tts_startup_error = str(exc)


def _create_rate_limiter(settings, redis_client, runtime_metrics=None):
    if settings.redis_rate_limit_requested and redis_client is not None:
        return RedisRateLimiter(redis_client, settings, runtime_metrics=runtime_metrics)
    return InMemoryRateLimiter(runtime_metrics=runtime_metrics)


async def _start_pubsub(app: FastAPI, settings, redis_client) -> None:
    if not bool(getattr(settings, "redis_pubsub_enabled", False)):
        _disable_pubsub(app)
        return
    if redis_client is None:
        _disable_pubsub(app, "Redis client is not initialized.")
        return
    fanout = RedisPubSubFanout(settings, redis_client, runtime_metrics=app.state.runtime_metrics)
    fanout.register_caption_handler(app.state.caption_hub.deliver_caption)
    fanout.register_debug_handler(app.state.caption_hub.deliver_debug)
    fanout.register_question_handler(app.state.question_hub.deliver)
    app.state.caption_hub.attach_pubsub(fanout)
    app.state.question_hub.attach_pubsub(fanout)
    await fanout.start()
    app.state.pubsub = fanout
    app.state.pubsub_status = fanout.status


async def _stop_pubsub(app: FastAPI) -> None:
    fanout = getattr(app.state, "pubsub", None)
    if fanout is not None:
        await fanout.stop()
    _disable_pubsub(app)


def _disable_pubsub(app: FastAPI, error: str | None = None) -> None:
    settings = app.state.settings
    pubsub_enabled = bool(getattr(settings, "redis_pubsub_enabled", False))
    app.state.pubsub = None
    app.state.pubsub_status = PubSubStatus(
        enabled=pubsub_enabled,
        connected=False,
        error=error if pubsub_enabled else None,
    )
    if hasattr(app.state, "caption_hub"):
        app.state.caption_hub.attach_pubsub(None)
    if hasattr(app.state, "question_hub"):
        app.state.question_hub.attach_pubsub(None)


def _install_exception_handlers(app: FastAPI) -> None:
    logger = logging.getLogger("app.errors")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(request, f"HTTP_{exc.status_code}", exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        details = exc.errors()
        return JSONResponse(
            status_code=422,
            content=_error_payload(request, "VALIDATION_ERROR", "Request validation failed.", details={"errors": sanitize_for_log(details)}),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        logger.exception(
            "unhandled_exception",
            extra={
                "event": {
                    "type": "unhandled_exception",
                    "path": request.url.path,
                    "request_id": getattr(request.state, "request_id", None),
                    "error": sanitize_for_log(str(exc)),
                }
            },
        )
        return JSONResponse(
            status_code=500,
            content=_error_payload(request, "INTERNAL_SERVER_ERROR", "Internal server error."),
        )


def _error_payload(request: Request, code: str, message, details: dict | None = None) -> dict:
    sanitized_message = sanitize_for_log(message)
    return {
        "detail": sanitized_message,
        "error": {
            "code": code,
            "message": sanitized_message if isinstance(sanitized_message, str) else "Request failed.",
            "details": details or {},
        },
        "request_id": getattr(request.state, "request_id", None),
    }


app = create_app()
