from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select, text

from app.db.models import Lesson
from app.db.database import database_type_from_url
from app.infra import redis as redis_infra
from app.infra.pubsub import PubSubStatus
from app.monitoring.metrics import runtime_metrics_snapshot
from app.production import database_config_summary, production_config_check
from app.providers.quotas import enrich_provider_status, provider_quota_checks, provider_quota_warnings
from app.smoke.provider_status import provider_status

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health(request: Request) -> dict:
    return {
        "status": "ok",
        "env": request.app.state.settings.app_env,
        "database_type": database_type_from_url(request.app.state.settings.database_url),
    }


@router.get("/health/live")
def live(request: Request) -> dict:
    return {"status": "alive", "env": request.app.state.settings.app_env}


@router.get("/health/ready")
async def ready(request: Request) -> dict:
    database_status = _database_status(request)
    database_type = _database_type(request)
    database = database_config_summary(request.app.state.settings, connection_ok=database_status == "ok")
    redis_status = await _redis_status(request)
    config_check = _config_check_with_redis(request, redis_status)
    redis_ok = (not redis_status.required) or redis_status.connected
    redis_rate_limit_status = _redis_rate_limit_status(request, redis_status)
    redis_rate_limit_ok = _redis_rate_limit_ready(request, redis_rate_limit_status)
    pubsub_status = _pubsub_status(request)
    pubsub_ok = _pubsub_ready(request, pubsub_status)
    ready_ok = (
        database_status == "ok"
        and database["production_ready"]
        and config_check["status"] == "ok"
        and redis_ok
        and redis_rate_limit_ok
        and pubsub_ok
    )
    status = "ready" if ready_ok else "not_ready"
    return {
        "status": status,
        "env": request.app.state.settings.app_env,
        "database": database,
        "database_status": database_status,
        "database_type": database_type,
        "config": "ok" if config_check["status"] == "ok" else "error",
        "config_missing": config_check["missing"],
        "config_warnings": config_check["warnings"],
        "redis": redis_status.to_dict(),
        "redis_rate_limit_enabled": redis_rate_limit_status["enabled"],
        "redis_rate_limit": redis_rate_limit_status,
        "redis_pubsub": pubsub_status.to_dict(),
        "providers": _provider_summary(request),
    }


@router.get("/health/providers")
def health_providers(request: Request) -> dict:
    return _provider_summary(request)


@router.get("/system/config-check")
async def config_check(request: Request) -> dict:
    redis_status = await _redis_status(request)
    return _config_check_with_redis(request, redis_status)


@router.get("/system/tasks")
def system_tasks(request: Request) -> dict:
    settings = request.app.state.settings
    if not settings.debug_endpoints_allowed:
        raise HTTPException(status_code=403, detail="Debug system endpoints are disabled in production.")
    return {"tasks": request.app.state.task_registry.list()}


@router.get("/metrics")
def metrics(request: Request) -> dict:
    caption_hub = request.app.state.caption_hub
    rtms_manager = request.app.state.rtms_manager
    browser_audio_manager = request.app.state.browser_audio_manager
    session_manager = request.app.state.session_manager
    with request.app.state.database.session_factory() as session:
        row = session.execute(
            select(
                func.coalesce(func.sum(Lesson.captions_sent), 0),
                func.coalesce(func.sum(Lesson.audio_chunks_received), 0),
                func.coalesce(func.sum(Lesson.audio_chunks_dropped), 0),
                func.coalesce(func.sum(Lesson.stt_provider_errors_count), 0),
                func.coalesce(func.sum(Lesson.translation_errors_count), 0),
            )
        ).one()
    return {
        "active_lessons": len(getattr(session_manager, "sessions", {})),
        "active_rtms_sessions": len(getattr(rtms_manager, "clients", {})),
        "active_browser_audio_connections": getattr(browser_audio_manager, "active_connections", 0),
        "active_websockets": _hub_count(getattr(caption_hub, "_caption_clients", {})),
        "active_diagnostic_websockets": _hub_count(getattr(caption_hub, "_debug_clients", {})),
        "captions_sent_total": int(row[0] or 0),
        "audio_chunks_received_total": int(row[1] or 0),
        "audio_chunks_dropped_total": int(row[2] or 0),
        "browser_audio_chunks_received_total": getattr(browser_audio_manager, "chunks_received_total", 0),
        "browser_audio_chunks_dropped_total": getattr(browser_audio_manager, "chunks_dropped_total", 0),
        "provider_errors_total": {
            "stt": int(row[3] or 0),
            "translation": int(row[4] or 0),
        },
        "queue_sizes": {lesson_id: queue.qsize() for lesson_id, queue in getattr(rtms_manager, "audio_queues", {}).items()},
        "browser_audio_queue_sizes": {lesson_id: queue.qsize() for lesson_id, queue in getattr(browser_audio_manager, "queues", {}).items()},
        "runtime": runtime_metrics_snapshot(request.app),
    }


def _database_status(request: Request) -> str:
    try:
        with request.app.state.database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        return "error"


def _database_type(request: Request) -> str:
    database = request.app.state.database
    if getattr(database, "database_type", None):
        return database.database_type
    if getattr(getattr(database, "engine", None), "dialect", None) is not None:
        dialect_name = database.engine.dialect.name
        return "postgresql" if dialect_name == "postgresql" else dialect_name
    return database_type_from_url(request.app.state.settings.database_url)


def _provider_summary(request: Request) -> dict:
    status = enrich_provider_status(provider_status(request.app.state.settings), request.app.state.settings, request.app)
    quota_warnings = provider_quota_warnings(status)
    summary = {
        "zoom_api": _ready_label(status["zoom"]["api"]),
        "zoom_meeting_sdk": _ready_label(status["zoom"]["meeting_sdk"]),
        "browser_audio": "ready" if status["browser_audio"]["ready"] else "disabled",
        "webhook": "configured" if status["zoom"]["webhook"]["configured"] else "missing_public_url",
        "stt": {name: _ready_label(payload) for name, payload in status["stt"].items()},
        "translation": {name: _ready_label(payload) for name, payload in status["translation"].items()},
        "tts": {name: _ready_label(payload) for name, payload in status.get("tts", {}).items()},
        "provider_capacity": "warning" if quota_warnings else "ok",
        "provider_quota_warnings": quota_warnings,
    }
    if "rtms" in status["zoom"]:
        summary["zoom_rtms"] = _ready_label(status["zoom"]["rtms"])
    summary["redis"] = getattr(request.app.state, "redis_status", None).to_dict() if getattr(request.app.state, "redis_status", None) else None
    summary["redis_rate_limit"] = _redis_rate_limit_status(request, getattr(request.app.state, "redis_status", None))
    summary["redis_pubsub"] = _pubsub_status(request).to_dict()
    return summary


def _ready_label(payload: dict) -> str:
    return "ready" if payload.get("ready") else "missing_credentials"


def _hub_count(clients: dict) -> int:
    return sum(len(items) for items in clients.values())


async def _redis_status(request: Request):
    settings = request.app.state.settings
    status = await redis_infra.redis_client_status(settings, getattr(request.app.state, "redis", None))
    request.app.state.redis_status = status
    return status


def _config_check_with_redis(request: Request, redis_status) -> dict:
    settings = request.app.state.settings
    payload = production_config_check(settings)
    payload["checks"]["redis"] = redis_status.to_dict()
    redis_rate_limit_status = _redis_rate_limit_status(request, redis_status)
    payload["checks"]["redis_rate_limit"] = redis_rate_limit_status
    pubsub_status = _pubsub_status(request)
    payload["checks"]["redis_pubsub"] = pubsub_status.to_dict()
    if redis_status.required and not redis_status.connected:
        payload["status"] = "error"
        if "REDIS_AVAILABLE" not in payload["missing"]:
            payload["missing"].append("REDIS_AVAILABLE")
    if not _pubsub_ready(request, pubsub_status):
        payload["status"] = "error"
        if "REDIS_PUBSUB_AVAILABLE" not in payload["missing"]:
            payload["missing"].append("REDIS_PUBSUB_AVAILABLE")
    if not _redis_rate_limit_ready(request, redis_rate_limit_status):
        payload["status"] = "error"
        if "REDIS_RATE_LIMIT_AVAILABLE" not in payload["missing"]:
            payload["missing"].append("REDIS_RATE_LIMIT_AVAILABLE")
    elif redis_rate_limit_status["enabled"] and not redis_rate_limit_status["connected"]:
        warning = "REDIS_RATE_LIMIT_DEGRADED"
        if warning not in payload["warnings"]:
            payload["warnings"].append(warning)
    providers = enrich_provider_status(provider_status(settings), settings, request.app)
    payload["checks"]["provider_quotas"] = provider_quota_checks(providers)
    for warning in provider_quota_warnings(providers):
        if warning not in payload["warnings"]:
            payload["warnings"].append(warning)
    return payload


def _pubsub_status(request: Request) -> PubSubStatus:
    status = getattr(request.app.state, "pubsub_status", None)
    if status is not None:
        return status
    settings = request.app.state.settings
    return PubSubStatus(enabled=bool(getattr(settings, "redis_pubsub_enabled", False)), connected=False)


def _pubsub_ready(request: Request, status: PubSubStatus) -> bool:
    settings = request.app.state.settings
    if not bool(getattr(settings, "redis_pubsub_enabled", False)):
        return True
    if not bool(getattr(settings, "redis_pubsub_fail_closed", False)):
        return True
    return bool(status.connected)


def _redis_rate_limit_status(request: Request, redis_status) -> dict:
    settings = request.app.state.settings
    enabled = bool(getattr(settings, "redis_rate_limit_requested", False))
    rate_limiter = getattr(request.app.state, "rate_limiter", None)
    active_backend = "redis" if rate_limiter.__class__.__name__ == "RedisRateLimiter" else "memory"
    connected = bool(enabled and redis_status is not None and getattr(redis_status, "connected", False) and active_backend == "redis")
    fail_closed = bool(getattr(settings, "redis_rate_limit_fail_closed", False) or redis_infra.redis_required(settings))
    return {
        "enabled": enabled,
        "active_backend": active_backend,
        "connected": connected,
        "fail_closed": fail_closed,
        "required": fail_closed,
    }


def _redis_rate_limit_ready(request: Request, status: dict) -> bool:
    if not status["enabled"]:
        return True
    if not status["fail_closed"]:
        return True
    return bool(status["connected"])
