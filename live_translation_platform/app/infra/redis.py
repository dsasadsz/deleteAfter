from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class RedisClientStatus:
    enabled: bool
    required: bool
    connected: bool
    url_configured: bool
    latency_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


async def create_redis_client(settings: Any):
    if not settings.redis_enabled:
        return None
    from redis.asyncio import Redis

    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        # Health probes are bounded with asyncio.wait_for in ping_redis().
        # A short socket_timeout would also apply to blocking Pub/Sub reads
        # and can make an idle listener look broken after one quiet second.
        socket_timeout=None,
        decode_responses=True,
    )


async def close_redis_client(client: Any) -> None:
    if client is None:
        return
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close is None:
        return
    result = close()
    if result is not None:
        await result


async def ping_redis(client: Any, timeout: float) -> float:
    started_at = time.perf_counter()
    await asyncio.wait_for(client.ping(), timeout=timeout)
    return round((time.perf_counter() - started_at) * 1000, 2)


async def redis_client_status(settings: Any, client: Any) -> RedisClientStatus:
    if not settings.redis_enabled:
        return RedisClientStatus(
            enabled=False,
            required=redis_required(settings),
            connected=False,
            url_configured=bool(settings.redis_url),
        )
    if client is None:
        return RedisClientStatus(
            enabled=True,
            required=redis_required(settings),
            connected=False,
            url_configured=bool(settings.redis_url),
            error="Redis client is not initialized.",
        )
    try:
        latency_ms = await ping_redis(client, settings.redis_health_timeout_seconds)
    except Exception as exc:
        return RedisClientStatus(
            enabled=True,
            required=redis_required(settings),
            connected=False,
            url_configured=bool(settings.redis_url),
            error=sanitize_redis_error(exc, settings),
        )
    return RedisClientStatus(
        enabled=True,
        required=redis_required(settings),
        connected=True,
        url_configured=bool(settings.redis_url),
        latency_ms=latency_ms,
    )


def redis_error_status(settings: Any, error: Exception) -> RedisClientStatus:
    return RedisClientStatus(
        enabled=bool(settings.redis_enabled),
        required=redis_required(settings),
        connected=False,
        url_configured=bool(settings.redis_url),
        error=sanitize_redis_error(error, settings),
    )


def build_redis_key(settings: Any, *parts: str) -> str:
    cleaned = [str(settings.redis_prefix).strip(":")]
    cleaned.extend(str(part).strip(":") for part in parts if str(part).strip(":"))
    return ":".join(cleaned)


def redis_required(settings: Any) -> bool:
    return bool(settings.is_production and settings.redis_required_in_production)


def sanitize_redis_error(error: Exception, settings: Any) -> str:
    message = str(error)
    redis_url = getattr(settings, "redis_url", "")
    if redis_url:
        message = message.replace(redis_url, redact_redis_url(redis_url))
        parsed = urlsplit(redis_url)
        if parsed.password:
            message = message.replace(parsed.password, "[redacted]")
    return message


def redact_redis_url(redis_url: str) -> str:
    parsed = urlsplit(redis_url)
    if not parsed.password:
        return redis_url
    username = parsed.username or ""
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{username}:[redacted]@" if username else ":[redacted]@"
    return urlunsplit((parsed.scheme, f"{auth}{host}{port}", parsed.path, parsed.query, parsed.fragment))
