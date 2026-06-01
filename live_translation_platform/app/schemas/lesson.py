from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


LessonMode = Literal["mock", "zoom"]
AudioSourceName = Literal["mock", "mock_audio", "zoom_rtms", "browser_ws"]
STTProviderName = Literal["mock", "elevenlabs", "azure", "cartesia", "faster_whisper"]
TranslationProviderName = Literal["mock", "azure", "google", "llm", "local"]


class LessonCreate(BaseModel):
    title: str = Field(default="C# lesson", min_length=1, max_length=255)
    mode: LessonMode = "mock"
    audio_source: AudioSourceName | None = None
    stt_provider: STTProviderName = "mock"
    translation_provider: TranslationProviderName = "mock"
    target_languages: list[str] = Field(default_factory=lambda: ["kk", "uz", "zh-Hans"])
    glossary_id: str | None = None
    glossary_enabled: bool = True


class ZoomLessonInfo(BaseModel):
    meeting_id: str
    meeting_uuid: str
    join_url: str
    start_url: str
    password: str = ""
    topic: str
    created_at: str | None = None


class LessonRead(BaseModel):
    lesson_id: str
    title: str
    mode: str
    status: str
    audio_source: str
    zoom_meeting_id: str
    zoom_meeting_uuid: str
    zoom_join_url: str
    zoom_start_url: str
    zoom_password: str
    zoom_topic: str
    zoom_created_at: str | None
    zoom: ZoomLessonInfo
    rtms_stream_id: str | None
    rtms_session_id: str | None
    rtms_started_at: datetime | None
    rtms_connected_at: datetime | None
    rtms_last_audio_at: datetime | None
    rtms_last_transcript_at: datetime | None
    rtms_error: str | None
    rtms_armed: bool = False
    rtms_armed_at: datetime | None = None
    audio_chunks_received: int
    transcript_events_received: int
    audio_chunks_dropped: int
    browser_audio_status: str
    browser_audio_connected_at: datetime | None
    browser_audio_last_chunk_at: datetime | None
    browser_audio_chunks_received: int
    browser_audio_bytes_received: int
    browser_audio_chunks_dropped: int
    browser_audio_error: str | None
    pipeline_status: str
    pipeline_audio_source: str | None
    pipeline_chunks_processed: int
    stt_events_generated: int
    captions_sent: int
    stt_provider_status: str
    stt_provider_connected_at: datetime | None
    stt_provider_audio_chunks_sent: int
    stt_provider_audio_bytes_sent: int
    stt_provider_partial_events: int
    stt_provider_final_events: int
    stt_provider_no_match_count: int
    stt_provider_canceled_count: int
    stt_provider_last_event_at: datetime | None
    stt_provider_errors_count: int
    stt_provider_last_error: str | None
    stt_provider_last_transcript: str | None
    translation_requests_count: int
    translation_errors_count: int
    translation_last_error: str | None
    translation_last_success_at: datetime | None
    translation_avg_latency_ms: float
    stt_provider: str
    translation_provider: str
    target_languages: list[str]
    glossary_id: str | None = None
    glossary_enabled: bool = True
    rtms_status: str
    connected_students: int
    created_at: datetime


class LessonActionResponse(BaseModel):
    lesson_id: str
    status: str
    rtms_status: str
