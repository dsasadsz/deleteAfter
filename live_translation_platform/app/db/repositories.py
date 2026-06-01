import json
from collections import defaultdict
from datetime import datetime
from statistics import mean

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    ComparisonRun,
    ComparisonRunItem,
    DebugEvent,
    E2EQATestRun,
    Glossary,
    GlossaryTerm,
    LatencyMetric,
    Lesson,
    LessonQuestion,
    LessonNotes,
    LiveMicTestRun,
    RealTestRun,
    SmokeTestEvent,
    SmokeTestRun,
    TranscriptSegment,
)
from app.glossary.schemas import GlossaryTermData


class LessonRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, lesson: Lesson) -> Lesson:
        self.session.add(lesson)
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def list(self) -> list[Lesson]:
        return list(self.session.scalars(select(Lesson).order_by(desc(Lesson.created_at))).all())

    def get(self, lesson_id: str) -> Lesson | None:
        return self.session.get(Lesson, lesson_id)

    def find_by_zoom(self, meeting_id: str | None = None, meeting_uuid: str | None = None) -> Lesson | None:
        if meeting_uuid:
            lesson = self.session.scalar(select(Lesson).where(Lesson.zoom_meeting_uuid == meeting_uuid))
            if lesson is not None:
                return lesson
        if meeting_id:
            return self.session.scalar(select(Lesson).where(Lesson.zoom_meeting_id == str(meeting_id)))
        return None

    def find_by_external_lesson_id(self, external_lesson_id: str) -> Lesson | None:
        return self.session.scalar(select(Lesson).where(Lesson.external_lesson_id == external_lesson_id))

    def update_status(self, lesson_id: str, status: str, rtms_status: str | None = None) -> None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return
        lesson.status = status
        if rtms_status is not None:
            lesson.rtms_status = rtms_status
        lesson.updated_at = datetime.utcnow()
        self.session.commit()

    def set_audio_source(self, lesson_id: str, audio_source: str) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        lesson.audio_source = audio_source
        if audio_source == "browser_ws":
            lesson.browser_audio_status = "waiting_for_teacher"
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def update_browser_audio(self, lesson_id: str, **fields) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        for key, value in fields.items():
            if hasattr(lesson, key):
                setattr(lesson, key, value)
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def update_rtms(self, lesson_id: str, **fields) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        for key, value in fields.items():
            if hasattr(lesson, key):
                setattr(lesson, key, value)
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def increment_rtms_audio(self, lesson_id: str, timestamp: datetime, status: str | None = None) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        lesson.audio_chunks_received += 1
        lesson.rtms_last_audio_at = timestamp
        if status:
            lesson.rtms_status = status
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def increment_rtms_audio_dropped(self, lesson_id: str) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        lesson.audio_chunks_dropped += 1
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def increment_rtms_transcript(self, lesson_id: str, timestamp: datetime, status: str | None = None) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        lesson.transcript_events_received += 1
        lesson.rtms_last_transcript_at = timestamp
        if status:
            lesson.rtms_status = status
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def record_pipeline_event(self, lesson_id: str, event: str, source: str | None = None) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        if event in {"starting", "running", "stopping", "stopped", "error", "degraded"}:
            lesson.pipeline_status = event
        elif event == "stt_disconnected":
            lesson.pipeline_status = "degraded"
        elif event == "audio_chunk_processed":
            lesson.pipeline_chunks_processed += 1
        elif event == "stt_event":
            lesson.stt_events_generated += 1
        elif event == "caption_sent":
            lesson.captions_sent += 1
        if source:
            lesson.pipeline_audio_source = source
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def record_stt_provider_metrics(self, lesson_id: str, provider) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        lesson.stt_provider_status = "connected" if getattr(provider, "connected_at", None) else "not_connected"
        lesson.stt_provider_connected_at = getattr(provider, "connected_at", None)
        lesson.stt_provider_audio_chunks_sent = getattr(provider, "audio_chunks_sent", lesson.stt_provider_audio_chunks_sent)
        lesson.stt_provider_audio_bytes_sent = getattr(provider, "audio_bytes_sent", lesson.stt_provider_audio_bytes_sent)
        lesson.stt_provider_partial_events = getattr(provider, "partial_events_received", lesson.stt_provider_partial_events)
        lesson.stt_provider_final_events = getattr(provider, "final_events_received", lesson.stt_provider_final_events)
        lesson.stt_provider_no_match_count = getattr(provider, "no_match_count", lesson.stt_provider_no_match_count)
        lesson.stt_provider_canceled_count = getattr(provider, "canceled_count", lesson.stt_provider_canceled_count)
        lesson.stt_provider_last_event_at = getattr(provider, "last_event_at", None)
        lesson.stt_provider_errors_count = getattr(provider, "errors_count", lesson.stt_provider_errors_count)
        lesson.stt_provider_last_error = getattr(provider, "last_error", None)
        lesson.stt_provider_last_transcript = getattr(provider, "last_transcript", None)
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def record_translation_provider_metrics(self, lesson_id: str, provider) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        lesson.translation_requests_count = getattr(provider, "translation_requests_count", lesson.translation_requests_count)
        lesson.translation_errors_count = getattr(provider, "translation_errors_count", lesson.translation_errors_count)
        lesson.translation_last_error = getattr(provider, "translation_last_error", None)
        lesson.translation_last_success_at = getattr(provider, "translation_last_success_at", None)
        lesson.translation_avg_latency_ms = getattr(provider, "translation_avg_latency_ms", lesson.translation_avg_latency_ms)
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def set_connected_students(self, lesson_id: str, count: int) -> None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return
        lesson.connected_students = count
        lesson.updated_at = datetime.utcnow()
        self.session.commit()

    def set_rtms_armed(self, lesson_id: str, armed: bool, status: str | None = None) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        lesson.rtms_armed = armed
        lesson.rtms_armed_at = datetime.utcnow() if armed else None
        if status is not None:
            lesson.rtms_status = status
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson

    def set_glossary(self, lesson_id: str, glossary_id: str | None, enabled: bool) -> Lesson | None:
        lesson = self.get(lesson_id)
        if lesson is None:
            return None
        lesson.glossary_id = glossary_id
        lesson.glossary_enabled = enabled
        lesson.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(lesson)
        return lesson


class LessonQuestionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_question(self, question: LessonQuestion) -> LessonQuestion:
        self.session.add(question)
        self.session.commit()
        self.session.refresh(question)
        return question

    def update_question(self, question_id: int, **fields) -> LessonQuestion | None:
        question = self.get_question(question_id)
        if question is None:
            return None
        for key, value in fields.items():
            if hasattr(question, key):
                setattr(question, key, value)
        self.session.commit()
        self.session.refresh(question)
        return question

    def list_questions_for_lesson(self, lesson_id: str) -> list[LessonQuestion]:
        return list(
            self.session.scalars(
                select(LessonQuestion).where(LessonQuestion.lesson_id == lesson_id).order_by(desc(LessonQuestion.created_at))
            ).all()
        )

    def mark_answered(self, question_id: int) -> LessonQuestion | None:
        return self.update_question(question_id, status="answered", answered_at=datetime.utcnow())

    def dismiss(self, question_id: int) -> LessonQuestion | None:
        return self.update_question(question_id, status="dismissed", dismissed_at=datetime.utcnow())

    def get_question(self, question_id: int) -> LessonQuestion | None:
        return self.session.get(LessonQuestion, question_id)


class TranscriptRepository:
    def __init__(self, session_factory: sessionmaker[Session], final_caption_capture=None) -> None:
        self.session_factory = session_factory
        self.final_caption_capture = final_caption_capture

    def save_final(self, payload: dict) -> None:
        with self.session_factory() as session:
            segment = TranscriptSegment(
                lesson_id=payload["lesson_id"],
                original_text=payload["original_text"],
                translations_json=json.dumps(payload.get("translations", {}), ensure_ascii=False),
                original_text_raw=payload.get("original_text_raw"),
                original_text_normalized=payload.get("original_text_normalized"),
                normalization_applied=bool(payload.get("glossary", {}).get("normalization_changes")),
                normalization_changes_json=json.dumps(payload.get("glossary", {}).get("normalization_changes", []), ensure_ascii=False),
                translation_postprocess_applied=bool(payload.get("glossary", {}).get("postprocess_changes")),
                translation_postprocess_changes_json=json.dumps(payload.get("glossary", {}).get("postprocess_changes", []), ensure_ascii=False),
                start_time=_parse_iso(payload.get("timestamps", {}).get("audio_received_at")),
                end_time=_parse_iso(payload.get("timestamps", {}).get("websocket_sent_at")),
                speaker_json=json.dumps(payload.get("speaker", {}), ensure_ascii=False),
                latency_json=json.dumps(payload.get("latency_ms", {}), ensure_ascii=False),
                is_final=payload.get("is_final", False),
                provider_stt=payload["provider"]["stt"],
                provider_translator=payload["provider"]["translator"],
            )
            session.add(segment)
            session.commit()
        if self.final_caption_capture is not None:
            self.final_caption_capture(payload)

    def latest(self, limit: int = 20) -> list[TranscriptSegment]:
        with self.session_factory() as session:
            return list(session.scalars(select(TranscriptSegment).order_by(desc(TranscriptSegment.created_at)).limit(limit)).all())

    def latest_for_lesson(self, lesson_id: str, limit: int = 20) -> list[TranscriptSegment]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.lesson_id == lesson_id)
                    .order_by(desc(TranscriptSegment.created_at))
                    .limit(limit)
                ).all()
            )


class MetricsRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save(self, payload: dict) -> None:
        latency = payload["latency_ms"]
        with self.session_factory() as session:
            metric = LatencyMetric(
                lesson_id=payload["lesson_id"],
                stt_ms=latency["stt"],
                translation_ms=latency["translation"],
                total_ms=latency["total"],
                provider_stt=payload["provider"]["stt"],
                provider_translator=payload["provider"]["translator"],
            )
            session.add(metric)
            session.commit()

    def averages_by_lesson(self) -> dict[str, dict[str, float]]:
        with self.session_factory() as session:
            rows = list(session.scalars(select(LatencyMetric)).all())
        grouped: dict[str, list[LatencyMetric]] = defaultdict(list)
        for row in rows:
            grouped[row.lesson_id].append(row)
        return {
            lesson_id: {
                "stt": round(mean(item.stt_ms for item in items), 1),
                "translation": round(mean(item.translation_ms for item in items), 1),
                "total": round(mean(item.total_ms for item in items), 1),
            }
            for lesson_id, items in grouped.items()
        }


class DebugRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def save(self, lesson_id: str, message: str, level: str = "info", payload: dict | None = None) -> None:
        with self.session_factory() as session:
            event = DebugEvent(
                lesson_id=lesson_id,
                level=level,
                message=message,
                payload_json=json.dumps(payload or {}, ensure_ascii=False, default=_json_default),
            )
            session.add(event)
            session.commit()

    def latest(self, limit: int = 20) -> list[DebugEvent]:
        with self.session_factory() as session:
            return list(session.scalars(select(DebugEvent).order_by(desc(DebugEvent.created_at)).limit(limit)).all())

    def latest_for_lesson(self, lesson_id: str, limit: int = 20) -> list[DebugEvent]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(DebugEvent)
                    .where(DebugEvent.lesson_id == lesson_id)
                    .order_by(desc(DebugEvent.created_at))
                    .limit(limit)
                ).all()
            )


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


class SmokeTestRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_run(
        self,
        lesson_id: str | None,
        stt_provider: str,
        translation_provider: str,
        audio_mode: str,
        target_languages: list[str],
        glossary_id: str | None = None,
        glossary_enabled: bool = False,
    ) -> SmokeTestRun:
        from uuid import uuid4

        run = SmokeTestRun(
            id=f"smoke_{uuid4().hex[:12]}",
            lesson_id=lesson_id,
            stt_provider=stt_provider,
            translation_provider=translation_provider,
            audio_mode=audio_mode,
            status="running",
            target_languages_json=json.dumps(target_languages, ensure_ascii=False),
            glossary_id=glossary_id,
            glossary_enabled=glossary_enabled,
            translations_json="{}",
            latency_json="{}",
            provider_metrics_json="{}",
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def get_run(self, smoke_test_id: str) -> SmokeTestRun | None:
        return self.session.get(SmokeTestRun, smoke_test_id)

    def latest_runs(self, limit: int = 10) -> list[SmokeTestRun]:
        return list(self.session.scalars(select(SmokeTestRun).order_by(desc(SmokeTestRun.started_at)).limit(limit)).all())

    def add_event(self, smoke_test_id: str, event_type: str, payload: dict) -> SmokeTestEvent:
        event = SmokeTestEvent(
            smoke_test_id=smoke_test_id,
            event_type=event_type,
            payload_json=json.dumps(payload, ensure_ascii=False, default=str),
        )
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)
        return event

    def events_for_run(self, smoke_test_id: str) -> list[SmokeTestEvent]:
        return list(
            self.session.scalars(
                select(SmokeTestEvent)
                .where(SmokeTestEvent.smoke_test_id == smoke_test_id)
                .order_by(SmokeTestEvent.created_at, SmokeTestEvent.id)
            ).all()
        )

    def mark_completed(
        self,
        smoke_test_id: str,
        original_text: str,
        translations: dict[str, str],
        latency_ms: dict[str, int],
        provider_metrics: dict,
    ) -> SmokeTestRun | None:
        run = self.get_run(smoke_test_id)
        if run is None:
            return None
        run.status = "completed"
        run.completed_at = datetime.utcnow()
        run.error = None
        run.original_text = original_text
        run.translations_json = json.dumps(translations, ensure_ascii=False)
        run.latency_json = json.dumps(latency_ms, ensure_ascii=False)
        run.provider_metrics_json = json.dumps(provider_metrics, ensure_ascii=False, default=str)
        self.session.commit()
        self.session.refresh(run)
        return run

    def mark_error(self, smoke_test_id: str, error: str, latency_ms: dict | None = None, provider_metrics: dict | None = None) -> SmokeTestRun | None:
        run = self.get_run(smoke_test_id)
        if run is None:
            return None
        run.status = "error"
        run.completed_at = datetime.utcnow()
        run.error = error
        if latency_ms is not None:
            run.latency_json = json.dumps(latency_ms, ensure_ascii=False)
        if provider_metrics is not None:
            run.provider_metrics_json = json.dumps(provider_metrics, ensure_ascii=False, default=str)
        self.session.commit()
        self.session.refresh(run)
        return run


class ComparisonRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_comparison(
        self,
        audio_mode: str,
        audio_sample_id: str | None,
        stt_providers: list[str],
        translation_provider: str,
        target_languages: list[str],
        run_mode: str,
        skipped: list[dict],
        glossary_id: str | None = None,
        glossary_enabled: bool = False,
    ) -> ComparisonRun:
        from uuid import uuid4

        comparison = ComparisonRun(
            id=f"cmp_{uuid4().hex[:12]}",
            audio_mode=audio_mode,
            audio_sample_id=audio_sample_id,
            stt_providers_json=json.dumps(stt_providers, ensure_ascii=False),
            translation_provider=translation_provider,
            target_languages_json=json.dumps(target_languages, ensure_ascii=False),
            glossary_id=glossary_id,
            glossary_enabled=glossary_enabled,
            run_mode=run_mode,
            status="running",
            skipped_json=json.dumps(skipped, ensure_ascii=False),
            summary_json="{}",
        )
        self.session.add(comparison)
        self.session.commit()
        self.session.refresh(comparison)
        return comparison

    def add_item(
        self,
        comparison_id: str,
        stt_provider: str,
        translation_provider: str,
        smoke_test_id: str | None = None,
        status: str = "pending",
    ) -> ComparisonRunItem:
        item = ComparisonRunItem(
            comparison_id=comparison_id,
            stt_provider=stt_provider,
            translation_provider=translation_provider,
            smoke_test_id=smoke_test_id,
            status=status,
        )
        self.session.add(item)
        self.session.commit()
        self.session.refresh(item)
        return item

    def update_item_status(
        self,
        item_id: int,
        status: str,
        smoke_test_id: str | None = None,
        error: str | None = None,
    ) -> ComparisonRunItem | None:
        item = self.session.get(ComparisonRunItem, item_id)
        if item is None:
            return None
        item.status = status
        if smoke_test_id is not None:
            item.smoke_test_id = smoke_test_id
        if error is not None:
            item.error = error
        if status in {"completed", "error", "skipped"}:
            item.completed_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(item)
        return item

    def update_result(self, item_id: int, result: dict, status: str = "completed", error: str | None = None) -> ComparisonRunItem | None:
        item = self.session.get(ComparisonRunItem, item_id)
        if item is None:
            return None
        item.result_json = json.dumps(result, ensure_ascii=False, default=str)
        item.status = status
        item.error = error
        item.completed_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(item)
        return item

    def complete_comparison(self, comparison_id: str, summary: dict, status: str = "completed", error: str | None = None) -> ComparisonRun | None:
        comparison = self.get_comparison(comparison_id)
        if comparison is None:
            return None
        comparison.status = status
        comparison.completed_at = datetime.utcnow()
        comparison.summary_json = json.dumps(summary, ensure_ascii=False, default=str)
        comparison.error = error
        self.session.commit()
        self.session.refresh(comparison)
        return comparison

    def get_comparison(self, comparison_id: str) -> ComparisonRun | None:
        return self.session.get(ComparisonRun, comparison_id)

    def items_for_comparison(self, comparison_id: str) -> list[ComparisonRunItem]:
        return list(
            self.session.scalars(
                select(ComparisonRunItem)
                .where(ComparisonRunItem.comparison_id == comparison_id)
                .order_by(ComparisonRunItem.id)
            ).all()
        )

    def list_recent_comparisons(self, limit: int = 10) -> list[ComparisonRun]:
        return list(self.session.scalars(select(ComparisonRun).order_by(desc(ComparisonRun.started_at)).limit(limit)).all())


class RealTestRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_run(
        self,
        lesson_id: str | None,
        selected_stt_provider: str,
        selected_translation_provider: str,
        readiness_snapshot: dict,
    ) -> RealTestRun:
        from uuid import uuid4

        run = RealTestRun(
            id=f"real_{uuid4().hex[:12]}",
            lesson_id=lesson_id,
            status="created",
            selected_stt_provider=selected_stt_provider,
            selected_translation_provider=selected_translation_provider,
            readiness_snapshot_json=json.dumps(readiness_snapshot, ensure_ascii=False, default=str),
            diagnostics_json="{}",
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def set_lesson(self, run_id: str, lesson_id: str, status: str = "lesson_created") -> RealTestRun | None:
        run = self.session.get(RealTestRun, run_id)
        if run is None:
            return None
        run.lesson_id = lesson_id
        run.status = status
        self.session.commit()
        self.session.refresh(run)
        return run

    def latest(self, limit: int = 10) -> list[RealTestRun]:
        return list(self.session.scalars(select(RealTestRun).order_by(desc(RealTestRun.started_at)).limit(limit)).all())


class LiveMicTestRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_run(
        self,
        lesson_id: str,
        stt_provider: str,
        translation_provider: str,
        chunk_ms: int,
        silence_commit_ms: int,
        max_segment_duration_ms: int,
        partials_enabled: bool,
        test_phrase_label: str,
        expected_text: str | None,
        tuning_snapshot: dict,
        audio_source: str = "browser_ws",
    ) -> LiveMicTestRun:
        from uuid import uuid4

        existing_runs = list(
            self.session.scalars(
                select(LiveMicTestRun).where(LiveMicTestRun.lesson_id == lesson_id, LiveMicTestRun.status == "active")
            ).all()
        )
        for existing in existing_runs:
            existing.status = "completed"
            existing.completed_by = "auto"
            existing.completed_at = datetime.utcnow()
            existing.updated_at = datetime.utcnow()

        run = LiveMicTestRun(
            id=f"live_{uuid4().hex[:12]}",
            lesson_id=lesson_id,
            status="active",
            audio_source=audio_source,
            stt_provider=stt_provider,
            translation_provider=translation_provider,
            chunk_ms=chunk_ms,
            silence_commit_ms=silence_commit_ms,
            max_segment_duration_ms=max_segment_duration_ms,
            partials_enabled=partials_enabled,
            test_phrase_label=test_phrase_label,
            expected_text=expected_text,
            tuning_snapshot_json=json.dumps(tuning_snapshot, ensure_ascii=False, default=str),
            provider_metrics_json="{}",
            last_caption_json="{}",
            translations_json="{}",
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def get(self, run_id: str) -> LiveMicTestRun | None:
        return self.session.get(LiveMicTestRun, run_id)

    def latest(self, limit: int = 50) -> list[LiveMicTestRun]:
        return list(self.session.scalars(select(LiveMicTestRun).order_by(desc(LiveMicTestRun.created_at)).limit(limit)).all())

    def active_for_lesson(self, lesson_id: str) -> LiveMicTestRun | None:
        return self.session.scalar(
            select(LiveMicTestRun)
            .where(LiveMicTestRun.lesson_id == lesson_id, LiveMicTestRun.status == "active")
            .order_by(desc(LiveMicTestRun.started_at))
        )

    def capture_caption(self, lesson_id: str, payload: dict) -> LiveMicTestRun | None:
        run = self.active_for_lesson(lesson_id)
        if run is None:
            return None
        latency = payload.get("latency_ms") or {}
        audio = payload.get("audio") or {}
        run.transcript = payload.get("original_text") or payload.get("original_text_normalized") or payload.get("original_text_raw")
        run.translations_json = json.dumps(payload.get("translations") or {}, ensure_ascii=False, default=str)
        run.first_partial_latency_ms = _int_or_none(latency.get("first_partial_latency_ms"))
        run.final_latency_ms = _int_or_none(latency.get("final_latency_ms"))
        run.translation_latency_ms = _int_or_none(latency.get("translation_latency_ms") or latency.get("translation"))
        run.total_latency_ms = _int_or_none(latency.get("total_latency_ms") or latency.get("total"))
        run.client_caption_latency_ms = _int_or_none(latency.get("client_caption_latency_ms"))
        run.chunks_sent = int(audio.get("chunks_sent") or payload.get("chunks_sent") or run.chunks_sent or 0)
        run.chunks_dropped = int(audio.get("dropped_chunks") or payload.get("dropped_chunks") or 0)
        run.commit_reason = payload.get("commit_reason") or audio.get("commit_reason")
        lesson = self.session.get(Lesson, lesson_id)
        run.provider_metrics_json = json.dumps(
            {
                "provider": payload.get("provider") or {},
                "lesson_counters": _lesson_provider_counters(lesson),
            },
            ensure_ascii=False,
            default=str,
        )
        run.last_caption_json = json.dumps(payload, ensure_ascii=False, default=str)
        run.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(run)
        return run

    def update_notes(self, run_id: str, transcript_quality: str | None, translation_quality: str | None, quality_notes: str | None) -> LiveMicTestRun | None:
        run = self.get(run_id)
        if run is None:
            return None
        run.transcript_quality = transcript_quality
        run.translation_quality = translation_quality
        run.quality_notes = quality_notes
        run.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(run)
        return run

    def finish(self, run_id: str, completed_by: str = "manual") -> LiveMicTestRun | None:
        run = self.get(run_id)
        if run is None:
            return None
        run.status = "completed"
        run.completed_by = completed_by
        run.completed_at = datetime.utcnow()
        run.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(run)
        return run


class E2EQATestRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_run(
        self,
        *,
        lesson_id: str | None,
        title: str,
        stt_provider: str,
        translation_provider: str,
        tts_provider: str,
        tts_language: str,
        tts_queue_mode: str,
        chunk_ms: int,
        silence_commit_ms: int,
        max_segment_duration_ms: int,
        partials_enabled: bool,
        checklist: dict,
        metrics: dict,
    ) -> E2EQATestRun:
        from uuid import uuid4

        run = E2EQATestRun(
            id=f"e2e_{uuid4().hex[:12]}",
            lesson_id=lesson_id,
            title=title,
            status="active",
            stt_provider=stt_provider,
            translation_provider=translation_provider,
            tts_provider=tts_provider,
            tts_language=tts_language,
            tts_queue_mode=tts_queue_mode,
            chunk_ms=chunk_ms,
            silence_commit_ms=silence_commit_ms,
            max_segment_duration_ms=max_segment_duration_ms,
            partials_enabled=partials_enabled,
            checklist_json=json.dumps(checklist, ensure_ascii=False, default=str),
            metrics_json=json.dumps(metrics, ensure_ascii=False, default=str),
        )
        self.session.add(run)
        self.session.commit()
        self.session.refresh(run)
        return run

    def get(self, run_id: str) -> E2EQATestRun | None:
        return self.session.get(E2EQATestRun, run_id)

    def latest(self, limit: int = 100) -> list[E2EQATestRun]:
        return list(self.session.scalars(select(E2EQATestRun).order_by(desc(E2EQATestRun.created_at)).limit(limit)).all())

    def update_checklist(self, run_id: str, key: str, status: str, notes: str | None = None) -> E2EQATestRun | None:
        run = self.get(run_id)
        if run is None:
            return None
        checklist = _loads_json(run.checklist_json, {})
        item = checklist.get(key)
        if item is None:
            return None
        item["status"] = status
        item["notes"] = notes or ""
        item["updated_at"] = datetime.utcnow().isoformat()
        run.checklist_json = json.dumps(checklist, ensure_ascii=False, default=str)
        run.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(run)
        return run

    def update_state(self, run_id: str, checklist: dict, metrics: dict) -> E2EQATestRun | None:
        run = self.get(run_id)
        if run is None:
            return None
        run.checklist_json = json.dumps(checklist, ensure_ascii=False, default=str)
        run.metrics_json = json.dumps(metrics, ensure_ascii=False, default=str)
        run.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(run)
        return run

    def finish(self, run_id: str, completed_by: str = "manual") -> E2EQATestRun | None:
        run = self.get(run_id)
        if run is None:
            return None
        run.status = "completed"
        run.completed_by = completed_by
        run.completed_at = datetime.utcnow()
        run.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(run)
        return run


class GlossaryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_glossary(
        self,
        name: str,
        description: str = "",
        domain: str = "",
        source_language: str = "ru-RU",
        target_languages: list[str] | None = None,
        is_default: bool = False,
        glossary_id: str | None = None,
    ) -> Glossary:
        from uuid import uuid4

        glossary = Glossary(
            id=glossary_id or f"glossary_{uuid4().hex[:12]}",
            name=name,
            description=description,
            domain=domain,
            source_language=source_language,
            target_languages_json=json.dumps(target_languages or [], ensure_ascii=False),
            is_default=is_default,
        )
        self.session.add(glossary)
        self.session.commit()
        self.session.refresh(glossary)
        return glossary

    def upsert_glossary_by_name(
        self,
        name: str,
        description: str,
        domain: str,
        source_language: str,
        target_languages: list[str],
        is_default: bool,
    ) -> Glossary:
        glossary = self.get_by_name(name)
        if glossary is None:
            return self.create_glossary(name, description, domain, source_language, target_languages, is_default)
        glossary.description = description
        glossary.domain = domain
        glossary.source_language = source_language
        glossary.target_languages_json = json.dumps(target_languages, ensure_ascii=False)
        glossary.is_default = is_default
        glossary.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(glossary)
        return glossary

    def list_glossaries(self) -> list[Glossary]:
        return list(self.session.scalars(select(Glossary).order_by(desc(Glossary.created_at))).all())

    def get_glossary(self, glossary_id: str) -> Glossary | None:
        return self.session.get(Glossary, glossary_id)

    def get_by_name(self, name: str) -> Glossary | None:
        return self.session.scalar(select(Glossary).where(Glossary.name == name))

    def update_glossary(self, glossary_id: str, **fields) -> Glossary | None:
        glossary = self.get_glossary(glossary_id)
        if glossary is None:
            return None
        for key, value in fields.items():
            if value is None:
                continue
            if key == "target_languages":
                glossary.target_languages_json = json.dumps(value, ensure_ascii=False)
            elif hasattr(glossary, key):
                setattr(glossary, key, value)
        glossary.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(glossary)
        return glossary

    def delete_glossary(self, glossary_id: str) -> bool:
        glossary = self.get_glossary(glossary_id)
        if glossary is None:
            return False
        for term in self.terms_for_glossary(glossary_id, enabled_only=False):
            self.session.delete(term)
        self.session.delete(glossary)
        self.session.commit()
        return True

    def create_term(
        self,
        glossary_id: str,
        source: str,
        canonical: str,
        aliases: list[str] | None = None,
        translations: dict[str, str] | None = None,
        case_sensitive: bool = False,
        match_type: str = "phrase",
        priority: int = 0,
        enabled: bool = True,
        term_id: str | None = None,
    ) -> GlossaryTerm:
        from uuid import uuid4

        term = GlossaryTerm(
            id=term_id or f"term_{uuid4().hex[:12]}",
            glossary_id=glossary_id,
            source=source,
            canonical=canonical,
            aliases_json=json.dumps(aliases or [], ensure_ascii=False),
            translations_json=json.dumps(translations or {}, ensure_ascii=False),
            case_sensitive=case_sensitive,
            match_type=match_type,
            priority=priority,
            enabled=enabled,
        )
        self.session.add(term)
        self.session.commit()
        self.session.refresh(term)
        return term

    def get_term(self, term_id: str) -> GlossaryTerm | None:
        return self.session.get(GlossaryTerm, term_id)

    def terms_for_glossary(self, glossary_id: str, enabled_only: bool = True) -> list[GlossaryTerm]:
        statement = select(GlossaryTerm).where(GlossaryTerm.glossary_id == glossary_id)
        if enabled_only:
            statement = statement.where(GlossaryTerm.enabled.is_(True))
        return list(self.session.scalars(statement.order_by(desc(GlossaryTerm.priority), GlossaryTerm.source)).all())

    def update_term(self, term_id: str, **fields) -> GlossaryTerm | None:
        term = self.get_term(term_id)
        if term is None:
            return None
        for key, value in fields.items():
            if value is None:
                continue
            if key == "aliases":
                term.aliases_json = json.dumps(value, ensure_ascii=False)
            elif key == "translations":
                term.translations_json = json.dumps(value, ensure_ascii=False)
            elif hasattr(term, key):
                setattr(term, key, value)
        term.updated_at = datetime.utcnow()
        self.session.commit()
        self.session.refresh(term)
        return term

    def delete_term(self, term_id: str) -> bool:
        term = self.get_term(term_id)
        if term is None:
            return False
        self.session.delete(term)
        self.session.commit()
        return True

    def term_data_for_glossary(self, glossary_id: str | None) -> list[GlossaryTermData]:
        if not glossary_id:
            return []
        return [term_to_data(term) for term in self.terms_for_glossary(glossary_id, enabled_only=True)]


def term_to_data(term: GlossaryTerm) -> GlossaryTermData:
    return GlossaryTermData(
        id=term.id,
        source=term.source,
        canonical=term.canonical,
        aliases=json.loads(term.aliases_json or "[]"),
        translations=json.loads(term.translations_json or "{}"),
        case_sensitive=term.case_sensitive,
        match_type=term.match_type,
        priority=term.priority,
        enabled=term.enabled,
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _loads_json(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _lesson_provider_counters(lesson: Lesson | None) -> dict:
    if lesson is None:
        return {}
    return {
        "stt_provider_status": lesson.stt_provider_status,
        "stt_provider_audio_chunks_sent": lesson.stt_provider_audio_chunks_sent,
        "stt_provider_partial_events": lesson.stt_provider_partial_events,
        "stt_provider_final_events": lesson.stt_provider_final_events,
        "stt_provider_errors_count": lesson.stt_provider_errors_count,
        "stt_provider_audio_bytes_sent": lesson.stt_provider_audio_bytes_sent,
        "translation_requests_count": lesson.translation_requests_count,
        "translation_errors_count": lesson.translation_errors_count,
        "translation_avg_latency_ms": lesson.translation_avg_latency_ms,
        "browser_audio_chunks_received": lesson.browser_audio_chunks_received,
        "browser_audio_chunks_dropped": lesson.browser_audio_chunks_dropped,
        "pipeline_chunks_processed": lesson.pipeline_chunks_processed,
        "captions_sent": lesson.captions_sent,
    }


class LessonNotesRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(self, lesson_id: str, language: str, mode: str, content_markdown: str, content_html: str, metadata: dict) -> LessonNotes:
        notes = LessonNotes(
            lesson_id=lesson_id,
            language=language,
            mode=mode,
            content_markdown=content_markdown,
            content_html=content_html,
            metadata_json=json.dumps(metadata, ensure_ascii=False, default=str),
        )
        self.session.add(notes)
        self.session.commit()
        self.session.refresh(notes)
        return notes
