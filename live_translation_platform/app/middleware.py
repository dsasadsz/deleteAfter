from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.production import sanitize_for_log

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    return request_id_var.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex}"
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["x-request-id"] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, logger_name: str = "app.access"):
        super().__init__(app)
        self.logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self.logger.info(
                "request_completed",
                extra={
                    "event": sanitize_for_log(
                        {
                            "type": "http_request",
                            "method": request.method,
                            "path": request.url.path,
                            "status_code": status_code,
                            "duration_ms": duration_ms,
                            "request_id": getattr(request.state, "request_id", None),
                        }
                    )
                },
            )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        settings = getattr(request.app.state, "settings", None)
        if settings is None or not getattr(settings, "security_headers_active", False):
            return response
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(self), geolocation=(), payment=(), usb=(), fullscreen=(self)",
        )
        if getattr(settings, "hsts_active", False):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


class RequestBodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = getattr(request.app.state, "settings", None)
        limit = int(getattr(settings, "max_request_body_bytes", 0) or 0)
        content_length = request.headers.get("content-length")
        if limit > 0 and content_length:
            try:
                size = int(content_length)
            except ValueError:
                size = 0
            if size > limit:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": "Request body too large.",
                        "error": {
                            "code": "REQUEST_BODY_TOO_LARGE",
                            "message": "Request body too large.",
                            "details": {"max_request_body_bytes": limit},
                        },
                        "request_id": getattr(request.state, "request_id", None),
                    },
                )
        return await call_next(request)
