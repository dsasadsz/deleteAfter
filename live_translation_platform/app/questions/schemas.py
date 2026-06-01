from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


SourceLanguage = Literal["kk", "uz", "zh-Hans", "ru", "auto"]


class TextQuestionRequest(BaseModel):
    student_id: str | None = None
    student_name: str | None = None
    source_language: SourceLanguage
    text: str = Field(min_length=1)


class QuestionRead(BaseModel):
    id: int
    lesson_id: str
    student_id: str | None
    student_name: str | None
    input_type: str
    source_language: str
    original_text: str
    translated_text_ru: str
    recognized_text: str | None
    status: str
    stt_provider: str | None
    translation_provider: str
    audio_duration_ms: int | None
    latency_ms: int | None
    error: str | None
    metadata_json: str | None
    created_at: datetime
    answered_at: datetime | None
    dismissed_at: datetime | None


class QuestionErrorEvent(BaseModel):
    event: Literal["question_error"] = "question_error"
    lesson_id: str
    code: str
    error: str
    question: QuestionRead | None = None
