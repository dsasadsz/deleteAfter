from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class Lesson(Base):
    __tablename__ = "lessons"

    lesson_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(32), default="mock")
    status: Mapped[str] = mapped_column(String(32), default="created")
    audio_source: Mapped[str] = mapped_column(String(64), default="mock")
    zoom_meeting_id: Mapped[str] = mapped_column(String(64), index=True)
    zoom_meeting_uuid: Mapped[str] = mapped_column(String(128), index=True)
    zoom_join_url: Mapped[str] = mapped_column(Text)
    zoom_start_url: Mapped[str] = mapped_column(Text)
    zoom_password: Mapped[str] = mapped_column(String(128), default="")
    zoom_topic: Mapped[str] = mapped_column(String(255), default="")
    zoom_created_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stt_provider: Mapped[str] = mapped_column(String(64), default="mock")
    translation_provider: Mapped[str] = mapped_column(String(64), default="mock")
    target_languages: Mapped[str] = mapped_column(String(128), default="kk,uz,zh-Hans")
    glossary_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    glossary_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rtms_status: Mapped[str] = mapped_column(String(64), default="not_configured")
    rtms_stream_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rtms_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rtms_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rtms_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rtms_last_audio_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rtms_last_transcript_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rtms_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    rtms_armed: Mapped[bool] = mapped_column(Boolean, default=False)
    rtms_armed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    audio_chunks_received: Mapped[int] = mapped_column(Integer, default=0)
    transcript_events_received: Mapped[int] = mapped_column(Integer, default=0)
    audio_chunks_dropped: Mapped[int] = mapped_column(Integer, default=0)
    browser_audio_status: Mapped[str] = mapped_column(String(64), default="not_connected")
    browser_audio_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    browser_audio_last_chunk_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    browser_audio_chunks_received: Mapped[int] = mapped_column(Integer, default=0)
    browser_audio_bytes_received: Mapped[int] = mapped_column(Integer, default=0)
    browser_audio_chunks_dropped: Mapped[int] = mapped_column(Integer, default=0)
    browser_audio_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pipeline_status: Mapped[str] = mapped_column(String(64), default="created")
    pipeline_audio_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pipeline_chunks_processed: Mapped[int] = mapped_column(Integer, default=0)
    stt_events_generated: Mapped[int] = mapped_column(Integer, default=0)
    captions_sent: Mapped[int] = mapped_column(Integer, default=0)
    stt_provider_status: Mapped[str] = mapped_column(String(64), default="not_connected")
    stt_provider_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stt_provider_audio_chunks_sent: Mapped[int] = mapped_column(Integer, default=0)
    stt_provider_partial_events: Mapped[int] = mapped_column(Integer, default=0)
    stt_provider_final_events: Mapped[int] = mapped_column(Integer, default=0)
    stt_provider_no_match_count: Mapped[int] = mapped_column(Integer, default=0)
    stt_provider_canceled_count: Mapped[int] = mapped_column(Integer, default=0)
    stt_provider_audio_bytes_sent: Mapped[int] = mapped_column(Integer, default=0)
    stt_provider_last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stt_provider_errors_count: Mapped[int] = mapped_column(Integer, default=0)
    stt_provider_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stt_provider_last_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    translation_requests_count: Mapped[int] = mapped_column(Integer, default=0)
    translation_errors_count: Mapped[int] = mapped_column(Integer, default=0)
    translation_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    translation_last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    translation_avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    connected_students: Mapped[int] = mapped_column(Integer, default=0)
    external_lesson_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    external_course_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_teacher_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    integration_metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LessonQuestion(Base):
    __tablename__ = "lesson_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[str] = mapped_column(String(64), index=True)
    student_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    student_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    input_type: Mapped[str] = mapped_column(String(16), default="text")
    source_language: Mapped[str] = mapped_column(String(32), default="auto")
    original_text: Mapped[str] = mapped_column(Text, default="")
    translated_text_ru: Mapped[str] = mapped_column(Text, default="")
    recognized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    stt_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    translation_provider: Mapped[str] = mapped_column(String(64), default="mock")
    audio_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[str] = mapped_column(String(64), index=True)
    original_text: Mapped[str] = mapped_column(Text)
    translations_json: Mapped[str] = mapped_column(Text)
    original_text_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_text_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalization_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    normalization_changes_json: Mapped[str] = mapped_column(Text, default="[]")
    translation_postprocess_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    translation_postprocess_changes_json: Mapped[str] = mapped_column(Text, default="[]")
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    speaker_json: Mapped[str] = mapped_column(Text, default="{}")
    latency_json: Mapped[str] = mapped_column(Text, default="{}")
    is_final: Mapped[bool] = mapped_column(Boolean, default=True)
    provider_stt: Mapped[str] = mapped_column(String(64))
    provider_translator: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class LatencyMetric(Base):
    __tablename__ = "latency_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[str] = mapped_column(String(64), index=True)
    stt_ms: Mapped[int] = mapped_column(Integer)
    translation_ms: Mapped[int] = mapped_column(Integer)
    total_ms: Mapped[int] = mapped_column(Integer)
    provider_stt: Mapped[str] = mapped_column(String(64))
    provider_translator: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DebugEvent(Base):
    __tablename__ = "debug_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[str] = mapped_column(String(64), index=True)
    level: Mapped[str] = mapped_column(String(32), default="info")
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SmokeTestRun(Base):
    __tablename__ = "smoke_test_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lesson_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stt_provider: Mapped[str] = mapped_column(String(64))
    translation_provider: Mapped[str] = mapped_column(String(64))
    audio_mode: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="running")
    target_languages_json: Mapped[str] = mapped_column(Text, default="[]")
    glossary_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    glossary_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    translations_json: Mapped[str] = mapped_column(Text, default="{}")
    latency_json: Mapped[str] = mapped_column(Text, default="{}")
    provider_metrics_json: Mapped[str] = mapped_column(Text, default="{}")


class SmokeTestEvent(Base):
    __tablename__ = "smoke_test_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    smoke_test_id: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ComparisonRun(Base):
    __tablename__ = "comparison_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    audio_mode: Mapped[str] = mapped_column(String(64))
    audio_sample_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stt_providers_json: Mapped[str] = mapped_column(Text, default="[]")
    translation_provider: Mapped[str] = mapped_column(String(64))
    target_languages_json: Mapped[str] = mapped_column(Text, default="[]")
    glossary_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    glossary_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    run_mode: Mapped[str] = mapped_column(String(32), default="sequential")
    status: Mapped[str] = mapped_column(String(32), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    skipped_json: Mapped[str] = mapped_column(Text, default="[]")
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ComparisonRunItem(Base):
    __tablename__ = "comparison_run_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    comparison_id: Mapped[str] = mapped_column(String(64), index=True)
    stt_provider: Mapped[str] = mapped_column(String(64))
    translation_provider: Mapped[str] = mapped_column(String(64))
    smoke_test_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RealTestRun(Base):
    __tablename__ = "real_test_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lesson_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="created")
    selected_stt_provider: Mapped[str] = mapped_column(String(64))
    selected_translation_provider: Mapped[str] = mapped_column(String(64))
    readiness_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    diagnostics_json: Mapped[str] = mapped_column(Text, default="{}")


class LiveMicTestRun(Base):
    __tablename__ = "live_mic_test_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    audio_source: Mapped[str] = mapped_column(String(64), default="browser_ws")
    stt_provider: Mapped[str] = mapped_column(String(64), index=True)
    translation_provider: Mapped[str] = mapped_column(String(64), index=True)
    chunk_ms: Mapped[int] = mapped_column(Integer, default=100)
    silence_commit_ms: Mapped[int] = mapped_column(Integer, default=1000)
    max_segment_duration_ms: Mapped[int] = mapped_column(Integer, default=6000)
    partials_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    test_phrase_label: Mapped[str] = mapped_column(String(128), default="short phrase", index=True)
    expected_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    tuning_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    provider_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    last_caption_json: Mapped[str] = mapped_column(Text, default="{}")
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    translations_json: Mapped[str] = mapped_column(Text, default="{}")
    first_partial_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    translation_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_caption_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunks_sent: Mapped[int] = mapped_column(Integer, default=0)
    chunks_dropped: Mapped[int] = mapped_column(Integer, default=0)
    commit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transcript_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    translation_quality: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quality_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class E2EQATestRun(Base):
    __tablename__ = "e2e_qa_test_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    lesson_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="Stage 22 manual QA")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    stt_provider: Mapped[str] = mapped_column(String(64), default="mock", index=True)
    translation_provider: Mapped[str] = mapped_column(String(64), default="mock", index=True)
    tts_provider: Mapped[str] = mapped_column(String(64), default="mock")
    tts_language: Mapped[str] = mapped_column(String(32), default="kk")
    tts_queue_mode: Mapped[str] = mapped_column(String(32), default="sequential")
    chunk_ms: Mapped[int] = mapped_column(Integer, default=100)
    silence_commit_ms: Mapped[int] = mapped_column(Integer, default=1000)
    max_segment_duration_ms: Mapped[int] = mapped_column(Integer, default=6000)
    partials_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    checklist_json: Mapped[str] = mapped_column(Text, default="{}")
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Glossary(Base):
    __tablename__ = "glossaries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    domain: Mapped[str] = mapped_column(String(64), default="")
    source_language: Mapped[str] = mapped_column(String(32), default="ru-RU")
    target_languages_json: Mapped[str] = mapped_column(Text, default="[]")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GlossaryTerm(Base):
    __tablename__ = "glossary_terms"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    glossary_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(Text)
    canonical: Mapped[str] = mapped_column(Text)
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    translations_json: Mapped[str] = mapped_column(Text, default="{}")
    case_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    match_type: Mapped[str] = mapped_column(String(16), default="phrase")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LessonNotes(Base):
    __tablename__ = "lesson_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[str] = mapped_column(String(64), index=True)
    language: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(32), default="simple")
    content_markdown: Mapped[str] = mapped_column(Text)
    content_html: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProviderPricing(Base):
    __tablename__ = "provider_pricing"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_type: Mapped[str] = mapped_column(String(32), index=True)
    provider_name: Mapped[str] = mapped_column(String(64), index=True)
    unit: Mapped[str] = mapped_column(String(64))
    price_per_unit: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_note: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UsageRecord(Base):
    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    smoke_test_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    comparison_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider_type: Mapped[str] = mapped_column(String(32), index=True)
    provider_name: Mapped[str] = mapped_column(String(64), index=True)
    metric_name: Mapped[str] = mapped_column(String(64), index=True)
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CostEstimate(Base):
    __tablename__ = "cost_estimates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lesson_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    smoke_test_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    comparison_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider_type: Mapped[str] = mapped_column(String(32), index=True)
    provider_name: Mapped[str] = mapped_column(String(64), index=True)
    usage_quantity: Mapped[float] = mapped_column(Float)
    usage_unit: Mapped[str] = mapped_column(String(64))
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    pricing_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
