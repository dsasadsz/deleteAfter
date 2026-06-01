from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class BrowserAudioStatus(StrEnum):
    NOT_CONNECTED = "not_connected"
    WAITING_FOR_TEACHER = "waiting_for_teacher"
    CONNECTED = "connected"
    RECEIVING_AUDIO = "receiving_audio"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class BrowserAudioConfig(BaseModel):
    enabled: bool = True
    queue_max_size: int = 200
    drop_policy: str = "drop_oldest"
    allow_duplicate_teacher: bool = False
    expected_sample_rate: int = 16000
    expected_channels: int = 1
    expected_format: str = "pcm_s16le"
    chunk_ms: int = 100
    use_audio_worklet: bool = True
    enable_resample_in_browser: bool = True
    commit_strategy: str = "vad"
    manual_commit_after_silence_ms: int = 800
    force_commit_enabled: bool = True
    partials_for_live: bool = True
    silence_rms_threshold: float = 0.01
    max_segment_duration_ms: int = 5000
    periodic_commit_enabled: bool = True


class BrowserAudioTuning(BaseModel):
    chunk_ms: int = Field(default=100, ge=20, le=500)
    commit_strategy: str = Field(default="vad", pattern="^(vad|manual)$")
    silence_commit_ms: int = Field(default=800, ge=0, le=10000)
    partials_enabled: bool = True
    force_commit_enabled: bool = True
    max_segment_duration_ms: int = Field(default=5000, ge=0, le=60000)
    rms_threshold: float = Field(default=0.01, ge=0.0, le=1.0)
    periodic_commit_enabled: bool = True
    last_updated_at: datetime | None = None
    updated_by: str | None = None


class BrowserAudioDebugEvent(BaseModel):
    event: str
    lesson_id: str
    level: str = "info"
    message: str = ""
    payload: dict = Field(default_factory=dict)
    created_at: datetime | None = None


class BrowserAudioStatusResponse(BaseModel):
    lesson_id: str
    status: BrowserAudioStatus = BrowserAudioStatus.NOT_CONNECTED
    connected_at: datetime | None = None
    last_audio_at: datetime | None = None
    chunks_received: int = 0
    chunks_yielded: int = 0
    chunks_dropped: int = 0
    bytes_received: int = 0
    queue_size: int = 0
    ws_connected: bool = False
    has_active_connection: bool = False
    active_connection_id: str | None = None
    latest_connection_id: str | None = None
    ws_ready_state: str | None = None
    metadata_received: bool = False
    metadata_received_at: datetime | None = None
    binary_frames_received: int = 0
    last_binary_frame_at: datetime | None = None
    first_audio_at: datetime | None = None
    last_error: str | None = None
    metadata: dict = Field(default_factory=dict)
    config: BrowserAudioConfig = Field(default_factory=BrowserAudioConfig)
    tuning: BrowserAudioTuning = Field(default_factory=BrowserAudioTuning)
