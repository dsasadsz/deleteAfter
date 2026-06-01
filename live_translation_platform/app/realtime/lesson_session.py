from dataclasses import dataclass

from app.audio.browser_mic_audio_source import BrowserMicAudioSource
from app.audio.mock_audio_source import MockAudioSource
from app.audio.zoom_rtms_audio_source import ZoomRTMSAudioSource
from app.db.models import Lesson
from app.db.repositories import DebugRepository, GlossaryRepository, LessonRepository, MetricsRepository, TranscriptRepository
from app.realtime.audio_pipeline import AudioPipeline
from app.realtime.caption_hub import CaptionHub
from app.stt.base import create_stt_provider
from app.translation.base import create_translation_provider


@dataclass
class LessonSession:
    lesson: Lesson
    pipeline: AudioPipeline
    running: bool = False

    async def start(self) -> None:
        if self.running:
            return
        await self.pipeline.start()
        self.running = True

    async def stop(self) -> None:
        if not self.running:
            return
        await self.pipeline.stop()
        self.running = False


class LessonSessionManager:
    def __init__(
        self,
        hub: CaptionHub,
        transcript_repo: TranscriptRepository,
        metrics_repo: MetricsRepository,
        debug_repo: DebugRepository,
        session_factory,
        rtms_manager,
        source_language: str,
        translate_partials: bool,
        browser_audio_manager=None,
        rtms_process_audio: bool = False,
        rtms_experimental_enabled: bool = False,
        mock_stt_audio_driven: bool = False,
        mock_stt_chunks_per_partial: int = 10,
        mock_stt_chunks_per_final: int = 30,
        mock_stt_min_final_interval_ms: int = 1200,
        audio_pipeline_queue_max_size: int = 200,
        audio_pipeline_drop_policy: str = "drop_oldest",
        elevenlabs_stt_config: dict | None = None,
        azure_stt_config: dict | None = None,
        cartesia_stt_config: dict | None = None,
        faster_whisper_stt_config: dict | None = None,
        azure_translator_config: dict | None = None,
        local_translation_config: dict | None = None,
        usage_tracker=None,
        runtime_metrics=None,
    ) -> None:
        self.hub = hub
        self.transcript_repo = transcript_repo
        self.metrics_repo = metrics_repo
        self.debug_repo = debug_repo
        self.session_factory = session_factory
        self.rtms_manager = rtms_manager
        self.browser_audio_manager = browser_audio_manager
        self.source_language = source_language
        self.translate_partials = translate_partials
        self.rtms_process_audio = rtms_process_audio
        self.rtms_experimental_enabled = rtms_experimental_enabled
        self.mock_stt_audio_driven = mock_stt_audio_driven
        self.mock_stt_chunks_per_partial = mock_stt_chunks_per_partial
        self.mock_stt_chunks_per_final = mock_stt_chunks_per_final
        self.mock_stt_min_final_interval_ms = mock_stt_min_final_interval_ms
        self.audio_pipeline_queue_max_size = audio_pipeline_queue_max_size
        self.audio_pipeline_drop_policy = audio_pipeline_drop_policy
        self.elevenlabs_stt_config = elevenlabs_stt_config or {}
        self.azure_stt_config = azure_stt_config or {}
        self.cartesia_stt_config = cartesia_stt_config or {}
        self.faster_whisper_stt_config = faster_whisper_stt_config or {}
        self.azure_translator_config = azure_translator_config or {}
        self.local_translation_config = local_translation_config or {}
        self.usage_tracker = usage_tracker
        self.runtime_metrics = runtime_metrics
        self.sessions: dict[str, LessonSession] = {}

    def get(self, lesson_id: str) -> LessonSession | None:
        return self.sessions.get(lesson_id)

    async def start(self, lesson: Lesson) -> LessonSession:
        existing = self.sessions.get(lesson.lesson_id)
        if existing is not None and existing.running:
            return existing

        self._record_pipeline_event(lesson.lesson_id, "starting", {"source": None})
        target_languages = [item for item in lesson.target_languages.split(",") if item]
        source = self._source_for_lesson(lesson)
        stt_kwargs = {}
        if lesson.stt_provider == "mock" and source.name in {"zoom_rtms", "browser_ws"}:
            stt_kwargs = {
                "audio_driven": self.mock_stt_audio_driven,
                "chunks_per_partial": self.mock_stt_chunks_per_partial,
                "chunks_per_final": self.mock_stt_chunks_per_final,
                "min_final_interval_ms": self.mock_stt_min_final_interval_ms,
            }
        elif lesson.stt_provider == "elevenlabs":
            stt_kwargs = dict(self.elevenlabs_stt_config)
        elif lesson.stt_provider == "azure":
            stt_kwargs = dict(self.azure_stt_config)
        elif lesson.stt_provider == "cartesia":
            stt_kwargs = dict(self.cartesia_stt_config)
        elif lesson.stt_provider == "faster_whisper":
            stt_kwargs = dict(self.faster_whisper_stt_config)
        translator_kwargs = {}
        if lesson.translation_provider == "azure":
            translator_kwargs = dict(self.azure_translator_config)
        elif lesson.translation_provider == "local":
            translator_kwargs = dict(self.local_translation_config)
        glossary_terms = self._glossary_terms(lesson)
        pipeline = AudioPipeline(
            lesson_id=lesson.lesson_id,
            meeting_id=lesson.zoom_meeting_id,
            source=source,
            stt=create_stt_provider(lesson.stt_provider, **stt_kwargs),
            translator=create_translation_provider(lesson.translation_provider, **translator_kwargs),
            target_languages=target_languages,
            translate_partials=self.translate_partials,
            publish=lambda payload: self.hub.broadcast_caption(lesson.lesson_id, payload),
            save_caption=self.transcript_repo.save_final,
            save_metric=self.metrics_repo.save,
            publish_debug=lambda payload: self._debug(lesson.lesson_id, payload),
            on_pipeline_event=lambda event, payload: self._record_pipeline_event(lesson.lesson_id, event, payload),
            source_language=self.source_language,
            glossary_terms=glossary_terms,
            glossary_id=lesson.glossary_id,
            glossary_enabled=lesson.glossary_enabled and bool(glossary_terms),
            usage_tracker=self.usage_tracker,
            queue_max_size=self.audio_pipeline_queue_max_size,
            drop_policy=self.audio_pipeline_drop_policy,
        )
        session = LessonSession(lesson=lesson, pipeline=pipeline)
        self.sessions[lesson.lesson_id] = session
        await session.start()
        return session

    async def stop(self, lesson_id: str) -> None:
        session = self.sessions.get(lesson_id)
        if session is None:
            return
        self._record_pipeline_event(lesson_id, "stopping", {})
        await session.stop()

    def _source_for_lesson(self, lesson: Lesson):
        audio_source = getattr(lesson, "audio_source", None)
        if not audio_source:
            audio_source = "mock"
        if audio_source == "browser_ws" and self.browser_audio_manager is not None:
            self.browser_audio_manager.prepare_lesson(lesson.lesson_id)
            return BrowserMicAudioSource(lesson.lesson_id, self.browser_audio_manager)
        if audio_source == "zoom_rtms" and self.rtms_experimental_enabled and self.rtms_process_audio and self.rtms_manager is not None:
            return ZoomRTMSAudioSource(lesson.lesson_id, self.rtms_manager.get_audio_queue(lesson.lesson_id))
        return MockAudioSource()

    def _glossary_terms(self, lesson: Lesson):
        if not lesson.glossary_enabled or not lesson.glossary_id:
            return []
        with self.session_factory() as session:
            return GlossaryRepository(session).term_data_for_glossary(lesson.glossary_id)

    def _record_pipeline_event(self, lesson_id: str, event: str, payload: dict) -> None:
        self._record_runtime_metric(event, payload)
        with self.session_factory() as session:
            repo = LessonRepository(session)
            if event == "stt_provider_metrics":
                repo.record_stt_provider_metrics(lesson_id, payload.get("provider"))
            elif event == "translation_provider_metrics":
                repo.record_translation_provider_metrics(lesson_id, payload.get("provider"))
            elif event == "pipeline_backpressure":
                repo.record_pipeline_event(lesson_id, event, payload.get("source"))
            elif event in {"stt_disconnected", "stt_commit_unsupported"}:
                repo.record_pipeline_event(lesson_id, event, payload.get("source"))
            else:
                repo.record_pipeline_event(lesson_id, event, payload.get("source"))

    def _record_runtime_metric(self, event: str, payload: dict) -> None:
        if self.runtime_metrics is None:
            return
        if event == "stt_disconnected":
            self.runtime_metrics.record_stt_disconnect()
        elif event == "provider_error":
            self.runtime_metrics.record_provider_error(payload.get("provider"), payload.get("error") or payload.get("error_class"))
        elif event == "translation_latency":
            self.runtime_metrics.record_translation_latency(payload.get("latency_ms"))

    async def _debug(self, lesson_id: str, payload: dict) -> None:
        self.debug_repo.save(lesson_id, payload["message"], payload.get("level", "info"), payload)
        await self.hub.broadcast_debug(lesson_id, payload)
