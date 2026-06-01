import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from app.db.models import Lesson


class ZoomMeetingSDKConfigurationError(RuntimeError):
    def __init__(self, message: str, code: str = "ZOOM_SIGNATURE_FAILED", details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class MeetingSDKConfig:
    client_id: str
    client_secret: str
    sdk_key: str = ""
    leave_url: str = "http://127.0.0.1:8000/"
    lang: str = "en-US"
    role_student: int = 0
    role_host: int = 1

    @property
    def public_key(self) -> str:
        return self.client_id or self.sdk_key

    @property
    def is_configured(self) -> bool:
        return bool(self.public_key and self.client_secret)


class ZoomMeetingSDKSignatureService:
    def __init__(self, config: MeetingSDKConfig) -> None:
        self.config = config

    def generate_signature(self, meeting_number: str, role: int, now_ms: int | None = None) -> str:
        if not self.config.is_configured:
            raise ZoomMeetingSDKConfigurationError(
                "Zoom Meeting SDK credentials are not configured.",
                code="ZOOM_SDK_NOT_CONFIGURED",
                details=self._configuration_details(None),
            )
        issued_at = int(((now_ms if now_ms is not None else int(time.time() * 1000)) - 30000) / 1000)
        expires_at = issued_at + 60 * 60 * 2
        payload = {
            "appKey": self.config.public_key,
            "sdkKey": self.config.public_key,
            "mn": str(meeting_number),
            "role": int(role),
            "iat": issued_at,
            "exp": expires_at,
            "tokenExp": expires_at,
        }
        header = {"alg": "HS256", "typ": "JWT"}
        signing_input = f"{_b64url_json(header)}.{_b64url_json(payload)}"
        digest = hmac.new(self.config.client_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
        return f"{signing_input}.{_b64url(digest)}"

    def build_embed_config(self, lesson: Lesson, user_name: str = "Student", role: int | None = None) -> dict:
        if lesson.mode == "mock":
            return {
                "mode": "mock",
                "lesson_id": lesson.lesson_id,
                "message": "Mock lesson uses the local video placeholder.",
            }
        if not lesson.zoom_meeting_id:
            raise ZoomMeetingSDKConfigurationError(
                "Lesson does not have a Zoom meeting number.",
                code="LESSON_ZOOM_NOT_READY",
                details=self._configuration_details(lesson),
            )
        if not self.config.is_configured:
            raise ZoomMeetingSDKConfigurationError(
                "Zoom Meeting SDK credentials are not configured.",
                code="ZOOM_SDK_NOT_CONFIGURED",
                details=self._configuration_details(lesson),
            )
        join_role = self.config.role_student if role is None else int(role)
        return {
            "mode": "zoom",
            "lesson_id": lesson.lesson_id,
            "meeting_number": lesson.zoom_meeting_id,
            "meeting_id": lesson.zoom_meeting_id,
            "meeting_uuid": lesson.zoom_meeting_uuid,
            "user_name": user_name or "Student",
            "role": join_role,
            "signature": self.generate_signature(lesson.zoom_meeting_id, join_role),
            "sdk_key_or_client_id": self.config.public_key,
            "password": lesson.zoom_password or "",
            "leave_url": self.config.leave_url,
            "lang": self.config.lang,
            "join_url": lesson.zoom_join_url,
        }

    def _configuration_details(self, lesson: Lesson | None) -> dict:
        missing = []
        if lesson is not None and not lesson.zoom_meeting_id:
            missing.append("zoom_meeting_id")
        if not self.config.public_key:
            missing.append("ZOOM_MEETING_SDK_KEY or ZOOM_SDK_KEY")
        if not self.config.client_secret:
            missing.append("ZOOM_MEETING_SDK_SECRET or ZOOM_SDK_SECRET")
        return {
            "lesson_id": lesson.lesson_id if lesson is not None else None,
            "has_zoom_meeting_id": bool(lesson and lesson.zoom_meeting_id),
            "has_zoom_password": bool(lesson and lesson.zoom_password),
            "has_sdk_key": bool(self.config.public_key),
            "missing": missing,
        }


def _b64url_json(payload: dict) -> str:
    return _b64url(json.dumps(payload, separators=(",", ":")).encode())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")
