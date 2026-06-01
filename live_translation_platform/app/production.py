from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from app.config import Settings
from app.db.database import database_type_from_url, database_url_configured

REDACTED = "[redacted]"
MULTI_WORKER_AUDIO_INGEST_WARNING = "MULTI_WORKER_AUDIO_INGEST_REQUIRES_STICKY_ROUTING"

SECRET_KEY_PARTS = (
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "password",
    "client_secret",
    "start_url",
    "zak",
    "integration_key",
    "cookie",
    "database_url",
    "provider_key",
)

SECRET_QUERY_RE = re.compile(
    r"([?&](?:token|access_token|refresh_token|api_key|apikey|key|password|pwd|signature|integration_key|integration-key|sig)=)[^&#\s\"']+",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
AUTH_HEADER_RE = re.compile(r"(?i)(Authorization\s*:\s*)(?:Bearer\s+)?[^\s,;]+")
COOKIE_HEADER_RE = re.compile(r"(?i)((?:Cookie|Set-Cookie)\s*:\s*)[^\r\n;]+(?:;[^\r\n]*)?")
USERINFO_URL_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/@\s]+:)[^/@\s]+(@)", re.IGNORECASE)


def sanitize_for_log(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized = {}
        for key, item in value.items():
            if _is_secret_key(str(key)):
                sanitized[key] = REDACTED
            else:
                sanitized[key] = sanitize_for_log(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_for_log(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_log(item) for item in value)
    if isinstance(value, str):
        return sanitize_secret_string(value)
    return value


def sanitize_secret_string(value: str) -> str:
    sanitized = BEARER_RE.sub(r"\1<redacted>", value)
    sanitized = AUTH_HEADER_RE.sub(r"\1<redacted>", sanitized)
    sanitized = COOKIE_HEADER_RE.sub(r"\1<redacted>", sanitized)
    sanitized = USERINFO_URL_RE.sub(r"\1<redacted>\2", sanitized)
    return SECRET_QUERY_RE.sub(r"\1<redacted>", sanitized)


def production_config_check(settings: Settings) -> dict:
    missing: list[str] = []
    warnings: list[str] = []
    database = database_config_summary(settings)
    teacher_audio_ingest = teacher_audio_ingest_config_summary(settings)

    if settings.is_production:
        required = {
            "PUBLIC_BASE_URL": settings.public_base_url,
            "CORS_ALLOWED_ORIGINS": settings.effective_allowed_origins_raw if settings.allowed_origin_list else "",
            "TRUSTED_HOSTS": settings.trusted_hosts,
            "DATABASE_URL": settings.database_url,
        }
        if settings.rtms_experimental_enabled or settings.zoom_rtms_enabled or settings.zoom_webhook_signature_required:
            required["ZOOM_WEBHOOK_SECRET_TOKEN"] = settings.zoom_webhook_secret_token
        missing.extend(name for name, value in required.items() if not value)
        if settings.enable_openapi_docs and not settings.docs_enabled:
            pass
        elif settings.enable_openapi_docs:
            warnings.append("ENABLE_OPENAPI_DOCS should usually be false in production.")
        if settings.enable_debug_endpoints and not settings.debug_endpoints_allowed:
            pass
        elif settings.enable_debug_endpoints:
            warnings.append("ENABLE_DEBUG_ENDPOINTS should be false in production.")
        if settings.effective_allowed_origins_raw.strip() == "*" and not settings.allow_wildcard_cors_in_production:
            if "CORS_ALLOWED_ORIGINS" not in missing:
                missing.append("CORS_ALLOWED_ORIGINS")
            warnings.append("Wildcard CORS is not allowed in production unless ALLOW_WILDCARD_CORS_IN_PRODUCTION=true.")
        if database["warning"]:
            warnings.append(database["warning"])
        if database["error"]:
            if database["type"] == "sqlite":
                missing.append("DATABASE_POSTGRESQL_REQUIRED")
            elif database["url_configured"]:
                missing.append("DATABASE_URL_VALID")
        if settings.log_format != "json":
            warnings.append("LOG_FORMAT=json is recommended in production.")
        if settings.integration_auth_enabled and not settings.integration_require_https:
            warnings.append("INTEGRATION_REQUIRE_HTTPS=true is recommended for production integration endpoints.")
        if settings.websocket_auth_required and not settings.security_signing_secret:
            missing.append("SECURITY_SIGNING_SECRET")
        if settings.redis_required_in_production and not settings.redis_enabled:
            missing.append("REDIS_ENABLED")
        if not teacher_audio_ingest["safe_deployment_mode"]:
            warnings.append(MULTI_WORKER_AUDIO_INGEST_WARNING)
            warnings.append(
                "Browser teacher audio ingest is process-local; multi-worker production deployments need sticky routing "
                "or distributed lesson sessions before enabling teacher audio ingest."
            )

    return {
        "status": "ok" if not missing else "error",
        "env": settings.app_env,
        "database_type": database["type"],
        "missing": missing,
        "warnings": warnings,
        "checks": {
            "public_base_url": bool(settings.public_base_url),
            "webhook_secret": bool(settings.zoom_webhook_secret_token),
            "webhook_signature_required": settings.zoom_webhook_signature_required,
            "cors_allowlist": bool(settings.allowed_origin_list),
            "cors_wildcard_allowed": bool(settings.allow_wildcard_cors_in_production),
            "trusted_hosts": bool(settings.trusted_host_list),
            "security_headers_enabled": settings.security_headers_active,
            "max_request_body_bytes": settings.max_request_body_bytes,
            "max_audio_upload_bytes": settings.max_audio_upload_bytes,
            "token_log_redaction_enabled": settings.token_log_redaction_enabled,
            "openapi_docs_enabled": settings.docs_enabled,
            "debug_endpoints_enabled": settings.debug_endpoints_allowed,
            "database": database,
            "postgres_required": settings.postgres_required_in_production and settings.is_production,
            "sqlite_allowed_in_production": settings.sqlite_allowed_in_production and settings.is_production,
            "redis": {
                "enabled": settings.redis_enabled,
                "required": settings.redis_required_in_production and settings.is_production,
                "url_configured": bool(settings.redis_url),
                "rate_limit_enabled": settings.redis_rate_limit_enabled,
                "rate_limit_fail_closed": settings.redis_rate_limit_fail_closed,
                "pubsub_enabled": settings.redis_pubsub_enabled,
                "pubsub_fail_closed": settings.redis_pubsub_fail_closed,
                "tts_cache_enabled": settings.redis_tts_cache_enabled,
            },
            "teacher_audio_ingest": teacher_audio_ingest,
        },
    }


def teacher_audio_ingest_config_summary(settings: Settings) -> dict:
    worker_count = max(1, settings.app_worker_count)
    sticky_routing = bool(settings.websocket_sticky_routing_enabled)
    distributed_sessions = bool(settings.distributed_lesson_sessions_enabled)
    safe_deployment_mode = bool(
        not settings.browser_audio_enabled
        or worker_count <= 1
        or sticky_routing
        or distributed_sessions
    )
    return {
        "enabled": settings.browser_audio_enabled,
        "worker_count": worker_count,
        "sticky_routing_enabled": sticky_routing,
        "distributed_lesson_sessions_enabled": distributed_sessions,
        "safe_deployment_mode": safe_deployment_mode,
    }


def database_config_summary(settings: Settings, *, connection_ok: bool | None = None) -> dict:
    database_url = settings.database_url
    url_configured = database_url_configured(database_url)
    raw_type = database_type_from_url(database_url)
    database_type = raw_type if raw_type in {"sqlite", "postgresql"} else "unknown"
    warning = None
    error = None
    production_ready = True

    if not url_configured:
        production_ready = False
        error = "DATABASE_URL is not configured."
    elif raw_type == "unknown":
        production_ready = False
        error = "DATABASE_URL is invalid or uses an unsupported database backend."
    elif settings.is_production and database_type == "sqlite":
        if settings.postgres_required_in_production or not settings.sqlite_allowed_in_production:
            production_ready = False
            error = "PostgreSQL is required in production; SQLite is only for local development and demos."
        else:
            warning = "SQLite is allowed by config, but PostgreSQL is recommended for production traffic."

    if connection_ok is False:
        production_ready = False
        if error is None:
            error = "Database connectivity check failed."

    return {
        "type": database_type,
        "url_configured": url_configured,
        "production_ready": production_ready,
        "warning": warning,
        "error": error,
    }


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SECRET_KEY_PARTS)
