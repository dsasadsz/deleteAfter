from enum import StrEnum

from pydantic import BaseModel


class RTMSStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    WAITING_FOR_MEETING = "waiting_for_meeting"
    WEBHOOK_RECEIVED = "webhook_received"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECEIVING_AUDIO = "receiving_audio"
    RECEIVING_TRANSCRIPT = "receiving_transcript"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class RTMSDebugEvent(BaseModel):
    event: str
    lesson_id: str
    status: str | None = None
    message: str
    payload: dict | None = None


class RTMSStatusResponse(BaseModel):
    lesson_id: str
    rtms_status: str
    rtms_stream_id: str | None
    rtms_session_id: str | None
    rtms_started_at: str | None
    rtms_connected_at: str | None
    rtms_last_audio_at: str | None
    rtms_last_transcript_at: str | None
    rtms_error: str | None
    rtms_armed: bool = False
    rtms_armed_at: str | None = None
    audio_chunks_received: int
    transcript_events_received: int
    audio_chunks_dropped: int
    audio_queue_size: int
    pipeline_status: str
    pipeline_audio_source: str | None
    pipeline_chunks_processed: int
    stt_events_generated: int
    captions_sent: int
    stt_provider_status: str
    stt_provider_connected_at: str | None
    stt_provider_audio_chunks_sent: int
    stt_provider_partial_events: int
    stt_provider_final_events: int
    stt_provider_last_event_at: str | None
    stt_provider_errors_count: int
    stt_provider_last_error: str | None
    translation_requests_count: int
    translation_errors_count: int
    translation_last_error: str | None
    translation_last_success_at: str | None
    translation_avg_latency_ms: float
