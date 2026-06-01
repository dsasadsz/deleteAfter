from datetime import datetime

from pydantic import BaseModel, Field


class TranscriptExportSegment(BaseModel):
    id: int
    start_time: str
    end_time: str
    speaker: str = "Teacher"
    original_text_raw: str
    original_text_normalized: str
    translations: dict[str, str] = Field(default_factory=dict)
    glossary: dict = Field(default_factory=dict)
    latency_ms: dict = Field(default_factory=dict)
    provider: dict = Field(default_factory=dict)
    is_final: bool = True
    created_at: datetime


class TranscriptExport(BaseModel):
    lesson_id: str
    title: str
    mode: str
    audio_source: str = "mock"
    providers: dict
    target_languages: list[str]
    glossary: dict
    segments: list[TranscriptExportSegment]
    summary: dict = Field(default_factory=dict)
    metrics: dict = Field(default_factory=dict)


class LessonNotesResult(BaseModel):
    lesson_id: str
    language: str
    mode: str
    content_markdown: str
    content_html: str
    metadata: dict = Field(default_factory=dict)
