from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from uuid import uuid4

from pydantic import ValidationError

from app.config import Settings
from app.security.schemas import TokenErrorCode, TokenPayload


class TokenError(Exception):
    def __init__(self, code: TokenErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def create_access_token(payload: dict, ttl_seconds: int) -> str:
    if ttl_seconds <= 0:
        ttl_seconds = 0
    now = int(time.time())
    claims = dict(payload)
    claims.setdefault("iat", now)
    claims["exp"] = now + ttl_seconds
    claims.setdefault("jti", uuid4().hex)
    validated = TokenPayload.model_validate(claims)
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_b64_json(header)}.{_b64_json(validated.model_dump(exclude_none=True))}"
    signature = _b64_bytes(_sign(signing_input.encode("ascii"), _signing_secret()))
    return f"{signing_input}.{signature}"


def verify_access_token(token: str | None) -> TokenPayload:
    if not token:
        raise TokenError(TokenErrorCode.TOKEN_MISSING, "Access token is missing.")
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError(TokenErrorCode.TOKEN_INVALID, "Access token is invalid.")
    signing_input = f"{parts[0]}.{parts[1]}"
    expected = _b64_bytes(_sign(signing_input.encode("ascii"), _signing_secret()))
    if not hmac.compare_digest(parts[2], expected):
        raise TokenError(TokenErrorCode.TOKEN_INVALID, "Access token is invalid.")
    try:
        header = _json_from_b64(parts[0])
        if header.get("alg") != "HS256":
            raise TokenError(TokenErrorCode.TOKEN_INVALID, "Access token is invalid.")
        payload = TokenPayload.model_validate(_json_from_b64(parts[1]))
    except (ValueError, ValidationError, UnicodeDecodeError, binascii.Error) as exc:
        raise TokenError(TokenErrorCode.TOKEN_INVALID, "Access token is invalid.") from exc
    if payload.exp <= int(time.time()):
        raise TokenError(TokenErrorCode.TOKEN_EXPIRED, "Access token has expired.")
    return payload


def require_scope(token_payload: TokenPayload, scope: str) -> TokenPayload:
    if scope not in token_payload.scopes:
        raise TokenError(TokenErrorCode.TOKEN_SCOPE_MISSING, "Access token scope is missing.")
    return token_payload


def require_lesson(token_payload: TokenPayload, lesson_id: str) -> TokenPayload:
    if token_payload.lesson_id != lesson_id:
        raise TokenError(TokenErrorCode.TOKEN_LESSON_MISMATCH, "Access token lesson does not match.")
    return token_payload


def _signing_secret() -> str:
    secret = Settings().security_signing_secret
    if not secret:
        raise TokenError(TokenErrorCode.TOKEN_INVALID, "Access token signing is not configured.")
    return secret


def _sign(value: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), value, hashlib.sha256).digest()


def _b64_json(value: dict) -> str:
    return _b64_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _b64_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _json_from_b64(value: str) -> dict:
    padding = "=" * (-len(value) % 4)
    return json.loads(base64.urlsafe_b64decode(value + padding).decode("utf-8"))
