import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ZoomWebhookContext:
    event: str | None
    meeting_id: str | None
    meeting_uuid: str | None
    rtms_stream_id: str | None
    rtms_session_id: str | None
    raw_payload: dict


@dataclass(frozen=True)
class ZoomWebhookSignatureValidationResult:
    valid: bool
    reason: str


def build_url_validation_response(plain_token: str, secret_token: str) -> dict:
    encrypted = hmac.new(secret_token.encode(), plain_token.encode(), hashlib.sha256).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": encrypted}


def extract_zoom_webhook_context(payload: dict) -> ZoomWebhookContext:
    event = payload.get("event")
    event_payload = _dict(payload.get("payload"))
    obj = _dict(event_payload.get("object"))
    root_obj = _dict(payload.get("object"))
    lookup = [event_payload, obj, root_obj, payload]

    return ZoomWebhookContext(
        event=event,
        meeting_id=_first_value(lookup, ["meeting_id", "id"]),
        meeting_uuid=_first_value(lookup, ["meeting_uuid", "uuid"]),
        rtms_stream_id=_first_value(lookup, ["rtms_stream_id", "stream_id"]),
        rtms_session_id=_first_value(lookup, ["rtms_session_id", "session_id"]),
        raw_payload=payload,
    )


def is_url_validation_event(payload: dict) -> bool:
    return payload.get("event") == "endpoint.url_validation"


def is_rtms_started_event(event: str | None) -> bool:
    return event in {"meeting.rtms_started", "meeting.rtms.started", "meeting.rtms_started-like"}


def is_rtms_stopped_event(event: str | None) -> bool:
    return event in {"meeting.rtms_stopped", "meeting.rtms.stopped", "meeting.rtms_stopped-like"}


def validate_zoom_webhook_signature(
    headers: Mapping[str, str],
    raw_body: bytes,
    secret_token: str,
    tolerance_seconds: int,
) -> ZoomWebhookSignatureValidationResult:
    if not secret_token:
        return ZoomWebhookSignatureValidationResult(False, "missing_secret")
    signature = headers.get("x-zm-signature") or headers.get("X-Zm-Signature")
    timestamp = headers.get("x-zm-request-timestamp") or headers.get("X-Zm-Request-Timestamp")
    if not signature or not timestamp:
        return ZoomWebhookSignatureValidationResult(False, "missing_headers")
    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return ZoomWebhookSignatureValidationResult(False, "invalid_timestamp")
    if abs(int(time.time()) - timestamp_int) > tolerance_seconds:
        return ZoomWebhookSignatureValidationResult(False, "timestamp_out_of_tolerance")
    message = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret_token.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return ZoomWebhookSignatureValidationResult(False, "invalid_signature")
    return ZoomWebhookSignatureValidationResult(True, "ok")


def verify_zoom_webhook_signature(headers: Mapping[str, str], raw_body: bytes, secret_token: str) -> bool:
    return validate_zoom_webhook_signature(headers, raw_body, secret_token, 300).valid


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _first_value(containers: list[dict], keys: list[str]) -> str | None:
    for container in containers:
        for key in keys:
            value = container.get(key)
            if value:
                return str(value)
    return None
