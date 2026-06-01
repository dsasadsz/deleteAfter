import time
from dataclasses import dataclass
from inspect import isawaitable
import logging
from typing import Any

from fastapi import HTTPException, Request, WebSocket

from app.infra.redis import build_redis_key, redis_required, sanitize_redis_error
from app.security.tokens import TokenError, verify_access_token


RATE_LIMIT_MESSAGE = "Too many requests, please wait."
RATE_LIMIT_UNAVAILABLE_MESSAGE = "Rate limiter is temporarily unavailable."
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int = 0
    backend_unavailable: bool = False
    backend_error: str | None = None


class InMemoryRateLimiter:
    def __init__(self, window_seconds: int = 60, runtime_metrics: Any | None = None) -> None:
        self.window_seconds = window_seconds
        self.runtime_metrics = runtime_metrics
        self._buckets: dict[str, tuple[float, int]] = {}

    def check(self, key: str, limit: int) -> RateLimitResult:
        self._record_check()
        now = time.monotonic()
        if limit < 0:
            return RateLimitResult(True)
        window_started_at, count = self._buckets.get(key, (now, 0))
        if now - window_started_at >= self.window_seconds:
            window_started_at = now
            count = 0
        if count >= limit:
            retry_after = max(1, int(self.window_seconds - (now - window_started_at)))
            self._record_blocked()
            return RateLimitResult(False, retry_after)
        self._buckets[key] = (window_started_at, count + 1)
        return RateLimitResult(True)

    def _record_check(self) -> None:
        if self.runtime_metrics is not None:
            self.runtime_metrics.record_rate_limit_check()

    def _record_blocked(self) -> None:
        if self.runtime_metrics is not None:
            self.runtime_metrics.record_rate_limit_blocked()


class RedisRateLimiter:
    def __init__(self, client: Any, settings: Any, window_seconds: int = 60, runtime_metrics: Any | None = None) -> None:
        self.client = client
        self.settings = settings
        self.window_seconds = window_seconds
        self.runtime_metrics = runtime_metrics

    async def check(self, key: str, limit: int) -> RateLimitResult:
        self._record_check()
        if limit < 0:
            return RateLimitResult(True)
        now = time.time()
        window_start = int(now // self.window_seconds) * self.window_seconds
        redis_key = self._redis_key(key, window_start)
        try:
            count = int(await self.client.incr(redis_key))
            if count == 1:
                await self.client.expire(redis_key, self.window_seconds)
        except Exception as exc:
            sanitized = sanitize_redis_error(exc, self.settings)
            self._record_redis_error()
            if self._fail_closed():
                self._record_blocked()
                return RateLimitResult(
                    False,
                    retry_after_seconds=self.window_seconds,
                    backend_unavailable=True,
                    backend_error=sanitized,
                )
            logger.warning("redis_rate_limit_unavailable_allowing_request", extra={"event": {"error": sanitized}})
            return RateLimitResult(True, backend_error=sanitized)
        if count > limit:
            retry_after = max(1, int(self.window_seconds - (now - window_start)))
            self._record_blocked()
            return RateLimitResult(False, retry_after_seconds=retry_after)
        return RateLimitResult(True)

    def _redis_key(self, key: str, window_start: int) -> str:
        scope, lesson_id, subject = _parse_rate_limit_key(key)
        return build_redis_key(self.settings, "rate", scope, lesson_id, subject, str(window_start))

    def _fail_closed(self) -> bool:
        return bool(getattr(self.settings, "redis_rate_limit_fail_closed", False) or redis_required(self.settings))

    def _record_check(self) -> None:
        if self.runtime_metrics is not None:
            self.runtime_metrics.record_rate_limit_check()

    def _record_blocked(self) -> None:
        if self.runtime_metrics is not None:
            self.runtime_metrics.record_rate_limit_blocked()

    def _record_redis_error(self) -> None:
        if self.runtime_metrics is not None:
            self.runtime_metrics.record_redis_rate_limit_error()


async def check_rate_limit(limiter: Any, key: str, limit: int) -> RateLimitResult:
    result = limiter.check(key, limit)
    if isawaitable(result):
        return await result
    return result


def rate_limit_key(scope: str, lesson_id: str, subject: str) -> str:
    return f"{scope}:lesson:{lesson_id}:{subject}"


def subject_for_request(request: Request, lesson_id: str, student_id: str | None = None) -> str:
    token_subject = _token_subject(_request_token(request), lesson_id)
    if token_subject:
        return f"token:{token_subject}"
    if student_id:
        return f"student:{student_id}"
    return f"ip:{_client_host(request)}"


def subject_for_websocket(websocket: WebSocket, lesson_id: str, student_id: str | None = None) -> str:
    token_subject = _token_subject(_websocket_token(websocket), lesson_id)
    if token_subject:
        return f"token:{token_subject}"
    if student_id:
        return f"student:{student_id}"
    client = websocket.client
    return f"ip:{client.host if client else 'unknown'}"


def rate_limit_http_exception(code: str, result: RateLimitResult) -> HTTPException:
    if result.backend_unavailable:
        return HTTPException(
            status_code=503,
            detail={
                "code": "RATE_LIMIT_UNAVAILABLE",
                "message": RATE_LIMIT_UNAVAILABLE_MESSAGE,
                "retry_after_seconds": result.retry_after_seconds,
            },
            headers={"Retry-After": str(result.retry_after_seconds)},
        )
    return HTTPException(
        status_code=429,
        detail={
            "code": code,
            "message": RATE_LIMIT_MESSAGE,
            "retry_after_seconds": result.retry_after_seconds,
        },
        headers={"Retry-After": str(result.retry_after_seconds)},
    )


def _token_subject(token: str | None, lesson_id: str) -> str | None:
    try:
        payload = verify_access_token(token)
    except TokenError:
        return None
    if payload.lesson_id != lesson_id:
        return None
    return payload.sub


def _request_token(request: Request) -> str | None:
    return request.query_params.get("token") or _bearer_token(request.headers.get("authorization"))


def _websocket_token(websocket: WebSocket) -> str | None:
    return (
        websocket.query_params.get("token")
        or _bearer_token(websocket.headers.get("authorization"))
        or websocket.headers.get("sec-websocket-protocol")
    )


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "Bearer "
    if value.startswith(prefix):
        return value[len(prefix) :]
    return None


def _client_host(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _parse_rate_limit_key(key: str) -> tuple[str, str, str]:
    scope, marker, lesson_id, subject = (key.split(":", 3) + ["", "", "", ""])[:4]
    if marker != "lesson" or not lesson_id:
        return scope or "unknown", "unknown", key
    return scope, lesson_id, subject or "unknown"
