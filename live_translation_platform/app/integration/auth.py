from __future__ import annotations

import logging
import re
import secrets
from uuid import uuid4

from fastapi import Header, HTTPException, Request, WebSocket

from app.middleware import current_request_id
from app.security.scopes import CAPTIONS_READ, QUESTION_MODERATE, QUESTION_READ, QUESTION_WRITE, TTS_PLAY, ZOOM_EMBED
from app.security.schemas import TokenErrorCode
from app.security.tokens import TokenError, require_lesson, require_scope, verify_access_token

logger = logging.getLogger("app.security")
_EMBED_CONFIG_RE = re.compile(r"^/api/v1/integration/lessons/([^/]+)/zoom/embed-config$")
_TTS_STATUS_RE = re.compile(r"^/api/v1/integration/lessons/([^/]+)/tts/status$")
_TTS_SYNTHESIZE_RE = re.compile(r"^/api/v1/integration/lessons/([^/]+)/tts/synthesize$")
_TTS_AUDIO_RE = re.compile(r"^/api/v1/integration/lessons/([^/]+)/tts/audio/[^/]+$")
_QUESTION_TEXT_RE = re.compile(r"^/api/v1/integration/lessons/([^/]+)/questions/text$")
_QUESTION_LIST_RE = re.compile(r"^/api/v1/integration/lessons/([^/]+)/questions$")
_QUESTION_MODERATE_RE = re.compile(r"^/api/v1/integration/lessons/([^/]+)/questions/[^/]+/(answer|dismiss)$")


def require_integration_key(request: Request, x_integration_key: str | None = Header(default=None)) -> None:
    settings = request.app.state.settings
    if not settings.integration_auth_enabled:
        return
    if _valid_key(x_integration_key, settings.integration_api_keys):
        return
    token_auth = _token_auth_for_path(request.url.path)
    if token_auth is not None:
        lesson_id, scopes = token_auth
        _require_request_token_any(request, lesson_id, scopes)
        return
    raise HTTPException(status_code=401, detail="Missing or invalid integration API key.")


async def require_websocket_integration_key(websocket: WebSocket) -> bool:
    settings = websocket.app.state.settings
    if not settings.integration_auth_enabled:
        return True
    candidates = [
        websocket.query_params.get("integration_key"),
        websocket.headers.get("x-integration-key"),
        _bearer_token(websocket.headers.get("authorization")),
        websocket.headers.get("sec-websocket-protocol"),
    ]
    if any(_valid_key(candidate, settings.integration_api_keys) for candidate in candidates):
        return True
    await websocket.close(code=1008)
    return False


async def authorize_websocket_access(
    websocket: WebSocket,
    lesson_id: str,
    scope: str,
    *,
    allow_integration_key: bool = False,
    allow_dev_bypass: bool = False,
) -> bool:
    settings = websocket.app.state.settings
    if allow_integration_key:
        if not getattr(settings, "integration_auth_enabled", True):
            return True
        if _websocket_has_valid_integration_key(websocket):
            return True
    token = _websocket_token(websocket)
    if token:
        try:
            payload = verify_access_token(token)
            require_lesson(payload, lesson_id)
            require_scope(payload, scope)
            return True
        except TokenError as exc:
            close_code = 4403 if exc.code in {TokenErrorCode.TOKEN_SCOPE_MISSING, TokenErrorCode.TOKEN_LESSON_MISMATCH} else 4401
            await _reject_websocket(websocket, close_code, lesson_id, scope, _websocket_token_error_reason(exc.code))
            return False
    if allow_dev_bypass and _dev_websocket_bypass_allowed(settings):
        logger.warning(
            "websocket_auth_dev_bypass",
            extra={
                "event": {
                    "type": "websocket_auth_dev_bypass",
                    "lesson_id": lesson_id,
                    "scope": scope,
                    "request_id": _websocket_request_id(websocket),
                }
            },
        )
        return True
    await _reject_websocket(websocket, 4401, lesson_id, scope, "WS_TOKEN_MISSING")
    return False


def _valid_key(candidate: str | None, valid_keys: list[str]) -> bool:
    if not candidate or not valid_keys:
        return False
    return any(secrets.compare_digest(candidate, key) for key in valid_keys)


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    prefix = "Bearer "
    if value.startswith(prefix):
        return value[len(prefix) :]
    return None


def _websocket_has_valid_integration_key(websocket: WebSocket) -> bool:
    settings = websocket.app.state.settings
    candidates = [
        websocket.query_params.get("integration_key"),
        websocket.headers.get("x-integration-key"),
        _bearer_token(websocket.headers.get("authorization")),
        websocket.headers.get("sec-websocket-protocol"),
    ]
    return any(_valid_key(candidate, settings.integration_api_keys) for candidate in candidates)


def _websocket_token(websocket: WebSocket) -> str | None:
    return (
        websocket.query_params.get("token")
        or _bearer_token(websocket.headers.get("authorization"))
        or websocket.headers.get("sec-websocket-protocol")
    )


def _dev_websocket_bypass_allowed(settings) -> bool:
    return (
        not getattr(settings, "is_production", False)
        and getattr(settings, "app_env", "").lower() == "development"
        and getattr(settings, "allow_dev_ws_without_token", True)
    )


async def _reject_websocket(websocket: WebSocket, code: int, lesson_id: str, scope: str, reason: str) -> None:
    logger.warning(
        "websocket_auth_rejected",
        extra={
            "event": {
                "type": "websocket_auth_rejected",
                "lesson_id": lesson_id,
                "scope": scope,
                "code": code,
                "reason": reason,
                "request_id": _websocket_request_id(websocket),
            }
        },
    )
    await websocket.close(code=code, reason=reason)


def _websocket_token_error_reason(code: TokenErrorCode) -> str:
    if code == TokenErrorCode.TOKEN_MISSING:
        return "WS_TOKEN_MISSING"
    if code == TokenErrorCode.TOKEN_SCOPE_MISSING:
        return "WS_TOKEN_SCOPE_MISSING"
    if code == TokenErrorCode.TOKEN_LESSON_MISMATCH:
        return "WS_TOKEN_LESSON_MISMATCH"
    if code == TokenErrorCode.TOKEN_EXPIRED:
        return "WS_TOKEN_EXPIRED"
    return "WS_TOKEN_INVALID"


def _websocket_request_id(websocket: WebSocket) -> str:
    return websocket.headers.get("x-request-id") or f"ws_{uuid4().hex}"


def _is_embed_config_request(request: Request) -> bool:
    return _lesson_id_from_embed_config_path(request.url.path) is not None


def _lesson_id_from_embed_config_path(path: str) -> str | None:
    match = _EMBED_CONFIG_RE.match(path)
    return match.group(1) if match else None


def _token_auth_for_path(path: str) -> tuple[str | None, list[str]] | None:
    if match := _EMBED_CONFIG_RE.match(path):
        return match.group(1), [ZOOM_EMBED]
    if match := _TTS_STATUS_RE.match(path):
        return match.group(1), [TTS_PLAY, CAPTIONS_READ]
    if match := _TTS_SYNTHESIZE_RE.match(path):
        return match.group(1), [TTS_PLAY]
    if match := _TTS_AUDIO_RE.match(path):
        return match.group(1), [TTS_PLAY]
    if match := _QUESTION_TEXT_RE.match(path):
        return match.group(1), [QUESTION_WRITE]
    if match := _QUESTION_LIST_RE.match(path):
        return match.group(1), [QUESTION_READ]
    if match := _QUESTION_MODERATE_RE.match(path):
        return match.group(1), [QUESTION_MODERATE]
    return None


def _require_request_token(request: Request, lesson_id: str | None, scope: str) -> None:
    _require_request_token_any(request, lesson_id, [scope])


def _require_request_token_any(request: Request, lesson_id: str | None, scopes: list[str]) -> None:
    if lesson_id is None:
        raise HTTPException(status_code=401, detail="Missing or invalid integration API key.")
    token = request.query_params.get("token") or _bearer_token(request.headers.get("authorization"))
    try:
        payload = verify_access_token(token)
        require_lesson(payload, lesson_id)
        if not any(scope in payload.scopes for scope in scopes):
            require_scope(payload, scopes[0])
    except TokenError as exc:
        status_code = 403 if exc.code in {TokenErrorCode.TOKEN_SCOPE_MISSING, TokenErrorCode.TOKEN_LESSON_MISMATCH} else 401
        logger.warning(
            "http_token_auth_rejected",
            extra={
                "event": {
                    "type": "http_token_auth_rejected",
                    "path": request.url.path,
                    "scopes": scopes,
                    "status_code": status_code,
                    "request_id": current_request_id(),
                }
            },
        )
        raise HTTPException(status_code=status_code, detail="Missing or invalid access token.") from exc
