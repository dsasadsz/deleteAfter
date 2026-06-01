from datetime import datetime

from pydantic import BaseModel


class ProviderInfo(BaseModel):
    stt: str
    translator: str


class Speaker(BaseModel):
    id: str
    name: str


class CaptionTimestamps(BaseModel):
    audio_received_at: datetime
    stt_result_at: datetime
    translation_done_at: datetime
    websocket_sent_at: datetime | None = None


class CaptionLatency(BaseModel):
    stt: int
    translation: int
    total: int


class CaptionEvent(BaseModel):
    event: str = "caption"
    lesson_id: str
    meeting_id: str
    provider: ProviderInfo
    source_language: str
    original_text: str
    translations: dict[str, str]
    is_partial: bool
    is_final: bool
    speaker: Speaker
    timestamps: CaptionTimestamps
    latency_ms: CaptionLatency

