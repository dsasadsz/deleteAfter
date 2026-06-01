from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class IntegrationLessonCreate(BaseModel):
    external_lesson_id: str
    external_course_id: str | None = None
    external_teacher_id: str | None = None
    external_tenant_id: str | None = None
    title: str
    mode: Literal["mock", "zoom"] = "zoom"
    audio_source: Literal["mock", "mock_audio", "zoom_rtms", "browser_ws"] | None = None
    stt_provider: Literal["mock", "elevenlabs", "azure", "cartesia", "faster_whisper"] = "mock"
    translation_provider: Literal["mock", "azure", "local"] = "mock"
    target_languages: list[str] = Field(default_factory=lambda: ["kk", "uz", "zh-Hans"])
    glossary_id: str | None = None
    glossary_enabled: bool = True
    create_zoom_meeting: bool = True
    callback_url: HttpUrl | None = None
    integration_metadata: dict = Field(default_factory=dict)


class IntegrationZoomInfo(BaseModel):
    meeting_id: str
    meeting_uuid: str
    join_url: str
    start_url: str
    password: str = ""


class IntegrationStudentInfo(BaseModel):
    captions_websocket_url: str
    diagnostics_websocket_url: str
    embed_config_url: str


class IntegrationLessonResponse(BaseModel):
    lesson_id: str
    external_lesson_id: str | None = None
    external_course_id: str | None = None
    external_teacher_id: str | None = None
    external_tenant_id: str | None = None
    mode: str
    audio_source: str
    status: str
    title: str
    stt_provider: str
    translation_provider: str
    target_languages: list[str]
    glossary_id: str | None = None
    glossary_enabled: bool = True
    zoom: IntegrationZoomInfo
    student: IntegrationStudentInfo


class IntegrationStatusResponse(BaseModel):
    lesson_id: str
    external_lesson_id: str | None = None
    lesson_status: str
    rtms_status: str
    pipeline_status: str
    audio_source: str
    stt: dict
    translation: dict
    captions: dict
    latency_ms: dict
    errors: list[dict] = Field(default_factory=list)


class IntegrationRTMSActionResponse(BaseModel):
    lesson_id: str
    rtms_status: str
    armed: bool


class IntegrationCallbackResult(BaseModel):
    ok: bool
    attempts: int
    status_code: int | None = None
    error: str | None = None


class IntegrationTextQuestionRequest(BaseModel):
    student_id: str | None = None
    student_name: str | None = None
    source_language: Literal["kk", "uz", "zh-Hans", "ru", "auto"]
    text: str = Field(min_length=1)


class IntegrationQuestionResponse(BaseModel):
    id: int
    lesson_id: str
    external_lesson_id: str | None = None
    student_id: str | None = None
    student_name: str | None = None
    input_type: Literal["text", "voice"]
    source_language: str
    original_text: str
    recognized_text: str | None = None
    translated_text_ru: str
    status: Literal["new", "answered", "dismissed", "error"]
    stt_provider: str | None = None
    translation_provider: str | None = None
    audio_duration_ms: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    created_at: str
    answered_at: str | None = None
    dismissed_at: str | None = None


class IntegrationQuestionListResponse(BaseModel):
    lesson_id: str
    external_lesson_id: str | None = None
    questions: list[IntegrationQuestionResponse]
