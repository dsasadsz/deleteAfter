from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.security.scopes import (
    AUDIO_WRITE,
    CAPTIONS_READ,
    DIAGNOSTICS_READ,
    QUESTION_MODERATE,
    QUESTION_READ,
    QUESTION_WRITE,
    TTS_PLAY,
    ZOOM_EMBED,
)


class TokenErrorCode(str, Enum):
    TOKEN_MISSING = "TOKEN_MISSING"
    TOKEN_INVALID = "TOKEN_INVALID"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_SCOPE_MISSING = "TOKEN_SCOPE_MISSING"
    TOKEN_LESSON_MISMATCH = "TOKEN_LESSON_MISMATCH"


class TokenPayload(BaseModel):
    sub: str
    role: Literal["teacher", "student", "admin", "integration"]
    lesson_id: str
    external_lesson_id: str | None = None
    scopes: list[str] = Field(default_factory=list)
    iat: int
    exp: int
    jti: str
    display_name: str | None = None


class StudentTokenRequest(BaseModel):
    external_student_id: str
    display_name: str = "Student"
    scopes: list[str] | None = Field(default_factory=lambda: [CAPTIONS_READ, ZOOM_EMBED, TTS_PLAY, QUESTION_WRITE, QUESTION_READ])
    ttl_seconds: int | None = Field(default=None, ge=1)


class TeacherTokenRequest(BaseModel):
    external_teacher_id: str
    display_name: str = "Teacher"
    scopes: list[str] | None = Field(default_factory=lambda: [AUDIO_WRITE, DIAGNOSTICS_READ, CAPTIONS_READ, QUESTION_READ, QUESTION_MODERATE])
    ttl_seconds: int | None = Field(default=None, ge=1)


class StudentTokenResponse(BaseModel):
    token: str
    expires_at: str
    lesson_id: str
    captions_websocket_url: str
    embed_config_url: str
    tts_status_url: str
    tts_synthesize_url: str
    questions_websocket_url: str
    text_question_url: str
    voice_question_audio_websocket_url: str


class TeacherTokenResponse(BaseModel):
    token: str
    expires_at: str
    lesson_id: str
    audio_ingest_websocket_url: str
    diagnostics_websocket_url: str
    questions_websocket_url: str
    questions_list_url: str
    question_answer_url_template: str
    question_dismiss_url_template: str
