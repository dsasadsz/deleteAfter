import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from app.audio.base import AudioChunk
from app.audio.mock_audio_source import MOCK_PHRASES
from app.config import Settings
from app.db.models import Lesson
from app.db.repositories import GlossaryRepository, LessonRepository, SmokeTestRepository
from app.glossary.normalizer import TranscriptNormalizer
from app.glossary.postprocessor import TranslationPostProcessor
from app.realtime.caption_hub import CaptionHub
from app.realtime.metrics import isoformat_z, milliseconds_between
from app.smoke.audio_samples import chunk_wav_file
from app.smoke.hub import SmokeEventHub
from app.smoke.provider_status import missing_for_selection
from app.stt.faster_whisper_stt import faster_whisper_provider_kwargs
from app.stt.base import STTEvent, create_stt_provider
from app.translation.base import create_translation_provider
from app.translation.local_provider import local_translation_provider_kwargs
from app.usage.usage_tracker import pcm_duration_seconds
from app.zoom.mock_zoom import MockZoomClient


class SmokeRunner:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker,
        smoke_hub: SmokeEventHub,
        caption_hub: CaptionHub,
        usage_tracker=None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = datetime.utcnow,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.smoke_hub = smoke_hub
        self.caption_hub = caption_hub
        self.usage_tracker = usage_tracker
        self._sleep = sleep
        self._clock = clock

    async def run(
        self,
        smoke_test_id: str,
        audio_sample_id: str | None = None,
        comparison_id: str | None = None,
        streaming_mode: str = "realtime_stream",
    ) -> None:
        with self.session_factory() as session:
            repo = SmokeTestRepository(session)
            run = repo.get_run(smoke_test_id)
            if run is None:
                return
            target_languages = json.loads(run.target_languages_json)
            glossary_id = run.glossary_id
            glossary_enabled = run.glossary_enabled
            missing = missing_for_selection(self.settings, run.stt_provider, run.translation_provider)
            if missing:
                await self._error(repo, smoke_test_id, f"Missing credentials: {', '.join(missing)}", {})
                return
            lesson_id = run.lesson_id

        try:
            if not lesson_id:
                lesson_id = await self._create_temporary_lesson(smoke_test_id, target_languages)
                with self.session_factory() as session:
                    run = SmokeTestRepository(session).get_run(smoke_test_id)
                    if run:
                        run.lesson_id = lesson_id
                        session.commit()

            chunks = self._chunks_for_mode(lesson_id, run.audio_mode, audio_sample_id)
            await self._execute(
                smoke_test_id,
                lesson_id,
                run.stt_provider,
                run.translation_provider,
                target_languages,
                chunks,
                run.audio_mode,
                glossary_id,
                glossary_enabled,
                comparison_id,
                streaming_mode,
            )
        except Exception as exc:
            with self.session_factory() as session:
                await self._error(SmokeTestRepository(session), smoke_test_id, str(exc) or exc.__class__.__name__, {})

    async def _execute(
        self,
        smoke_test_id: str,
        lesson_id: str,
        stt_provider_name: str,
        translation_provider_name: str,
        target_languages: list[str],
        chunks: list[AudioChunk],
        audio_mode: str,
        glossary_id: str | None = None,
        glossary_enabled: bool = False,
        comparison_id: str | None = None,
        streaming_mode: str = "realtime_stream",
    ) -> None:
        stt_kwargs = self._stt_kwargs(stt_provider_name)
        if stt_provider_name == "elevenlabs" and audio_mode == "wav_upload":
            stt_kwargs["commit_strategy"] = "manual"
        stt = create_stt_provider(stt_provider_name, **stt_kwargs)
        translator = create_translation_provider(translation_provider_name, **self._translator_kwargs(translation_provider_name))
        timestamps: dict[str, datetime] = {}
        results = {"original_text": "", "translations": {}}
        errors: list[str] = []
        audio_bytes = 0
        audio_duration_seconds = 0.0
        audio_streaming_metrics: dict = {"streaming_mode": streaming_mode, "commit_strategy": stt_kwargs.get("commit_strategy")}
        glossary_terms = self._glossary_terms(glossary_id) if glossary_enabled else []
        glossary_meta = {"enabled": glossary_enabled and bool(glossary_terms), "glossary_id": glossary_id, "normalization_changes": [], "postprocess_changes": []}

        await self._record(smoke_test_id, "smoke_started", {"lesson_id": lesson_id, "audio_mode": audio_mode, "streaming_mode": streaming_mode})
        await stt.connect()
        timestamps["provider_connected_at"] = self._clock()
        audio_streaming_metrics["provider_connected_at"] = isoformat_z(timestamps["provider_connected_at"])
        events_task = asyncio.create_task(self._collect_until_final(stt), name=f"{smoke_test_id}-stt-events")
        await self._send_audio_chunks(stt, chunks, timestamps, audio_streaming_metrics, streaming_mode, smoke_test_id, events_task)
        audio_bytes = sum(len(chunk.data) for chunk in chunks)
        audio_duration_seconds = sum(pcm_duration_seconds(len(chunk.data), chunk.sample_rate, chunk.channels, 16) for chunk in chunks)

        stt_events = await asyncio.wait_for(
            events_task,
            timeout=self._stt_timeout_seconds(stt_provider_name, streaming_mode, audio_duration_seconds),
        )
        for event in stt_events:
            if event.is_partial:
                timestamps.setdefault("stt_first_partial_at", event.timestamp)
                audio_streaming_metrics.setdefault("first_partial_received_at", isoformat_z(event.timestamp))
                await self._record(smoke_test_id, "stt_partial", {"text": event.text, "stt_partial_at": isoformat_z(event.timestamp)})
            if event.is_final:
                timestamps["stt_final_at"] = event.timestamp
                audio_streaming_metrics.setdefault("first_final_received_at", isoformat_z(event.timestamp))
                normalization = TranscriptNormalizer().normalize(event.text, glossary_terms) if glossary_meta["enabled"] else None
                results["original_text_raw"] = event.text
                results["original_text"] = normalization.normalized_text if normalization else event.text
                glossary_meta["normalization_changes"] = normalization.changes if normalization else []
                await self._record(
                    smoke_test_id,
                    "stt_final",
                    {
                        "text": event.text,
                        "normalized_text": results["original_text"],
                        "glossary": glossary_meta,
                        "stt_final_at": isoformat_z(event.timestamp),
                    },
                )

        if not results["original_text"]:
            raise RuntimeError("Smoke STT did not produce a final transcript.")

        translation_start = self._clock()
        translations = await translator.translate_many(results["original_text"], self.settings.source_language, target_languages)
        self._record_usage(
            stt.name,
            translator.name,
            lesson_id,
            smoke_test_id,
            comparison_id,
            audio_duration_seconds,
            audio_bytes,
            len(chunks),
            results["original_text"],
            target_languages,
            "browser_ws" if audio_mode == "direct_ws" else audio_mode,
        )
        if glossary_meta["enabled"]:
            postprocessed = TranslationPostProcessor().postprocess(results["original_text"], translations, glossary_terms)
            translations = postprocessed.translations
            glossary_meta["postprocess_changes"] = postprocessed.changes
        translation_done_at = self._clock()
        timestamps["translation_done_at"] = translation_done_at
        results["translations"] = translations
        await self._record(
            smoke_test_id,
            "translation_done",
            {
                "translations": translations,
                "glossary": glossary_meta,
                "translation_started_at": isoformat_z(translation_start),
                "translation_done_at": isoformat_z(translation_done_at),
            },
        )

        websocket_sent_at = self._clock()
        timestamps["websocket_sent_at"] = websocket_sent_at
        caption = self._caption_payload(
            lesson_id,
            stt.name,
            translator.name,
            "browser_ws" if audio_mode == "direct_ws" else audio_mode,
            results["original_text"],
            translations,
            timestamps,
            glossary_meta,
            results.get("original_text_raw", results["original_text"]),
            streaming_mode,
        )
        await self.caption_hub.broadcast_caption(lesson_id, caption)
        await self._record(
            smoke_test_id,
            "caption_sent",
            {
                "lesson_id": lesson_id,
                "websocket_sent_at": isoformat_z(websocket_sent_at),
                "latency_ms": caption["latency_ms"],
            },
        )
        await stt.close()
        audio_streaming_metrics["provider_closed_at"] = isoformat_z(getattr(stt, "provider_closed_at", None) or self._clock())

        provider_metrics = {
            "stt": self._provider_metrics(stt),
            "translator": self._provider_metrics(translator),
            "audio_streaming": audio_streaming_metrics,
            "errors": errors,
            "glossary": glossary_meta,
        }
        with self.session_factory() as session:
            repo = SmokeTestRepository(session)
            repo.mark_completed(
                smoke_test_id,
                original_text=results["original_text"],
                translations=translations,
                latency_ms=caption["latency_ms"],
                provider_metrics=provider_metrics,
            )
        await self._record(smoke_test_id, "smoke_completed", {"status": "completed", "latency_ms": caption["latency_ms"]})

    async def _send_audio_chunks(
        self,
        stt,
        chunks: list[AudioChunk],
        timestamps: dict[str, datetime],
        metrics: dict,
        streaming_mode: str,
        smoke_test_id: str | None = None,
        stop_when_done: asyncio.Task | None = None,
    ) -> None:
        chunk_ms = self.settings.smoke_audio_chunk_ms
        metrics.update(
            {
                "streaming_mode": streaming_mode,
                "chunks_count": len(chunks),
                "chunks_sent_count": 0,
                "chunk_ms": chunk_ms,
                "audio_duration_ms": int(
                    sum(pcm_duration_seconds(len(chunk.data), chunk.sample_rate, chunk.channels, 16) for chunk in chunks) * 1000
                ),
            }
        )
        chunks_to_send = _trim_trailing_silent_chunks(chunks) if streaming_mode == "realtime_stream" else chunks
        metrics["trimmed_trailing_chunks"] = len(chunks) - len(chunks_to_send)
        metrics["streamed_audio_duration_ms"] = int(
            sum(pcm_duration_seconds(len(chunk.data), chunk.sample_rate, chunk.channels, 16) for chunk in chunks_to_send) * 1000
        )
        for index, chunk in enumerate(chunks_to_send):
            if stop_when_done is not None and stop_when_done.done():
                break
            if streaming_mode == "realtime_stream" and index > 0:
                await self._sleep(chunk_ms / 1000)
                if stop_when_done is not None and stop_when_done.done():
                    break
            sent_at = self._clock()
            if "audio_injected_at" not in timestamps:
                timestamps["audio_injected_at"] = sent_at
                timestamps["first_chunk_sent_at"] = sent_at
                metrics["first_chunk_sent_at"] = isoformat_z(sent_at)
            timestamps["last_chunk_sent_at"] = sent_at
            metrics["last_chunk_sent_at"] = isoformat_z(sent_at)
            metadata = {
                **chunk.metadata,
                "audio_received_at": sent_at,
                "source": chunk.source,
                "sample_rate": chunk.sample_rate,
                "channels": chunk.channels,
                "format": chunk.format,
                "speaker_id": chunk.speaker_id,
                "finalize": index == len(chunks_to_send) - 1,
                "finalize_mode": "inline" if streaming_mode == "fast_upload" else "separate",
            }
            if index == 0:
                metrics["first_audio_chunk_provider_sent_at"] = isoformat_z(sent_at)
            metrics["last_audio_chunk_provider_sent_at"] = isoformat_z(sent_at)
            if index == len(chunks_to_send) - 1:
                metrics["finalize_sent_at"] = isoformat_z(sent_at)
            await stt.send_audio(chunk.data, metadata)
            metrics["chunks_sent_count"] = index + 1
            if smoke_test_id is not None:
                await self._record(
                    smoke_test_id,
                    "audio_chunk_sent",
                    {
                        "sequence": index,
                        "sample_rate": chunk.sample_rate,
                        "channels": chunk.channels,
                        "format": chunk.format,
                        "audio_injected_at": isoformat_z(sent_at),
                        "streaming_mode": streaming_mode,
                        "finalize": index == len(chunks_to_send) - 1,
                    },
                )
        completed_at = self._clock()
        timestamps["audio_send_completed_at"] = completed_at
        metrics["audio_send_completed_at"] = isoformat_z(completed_at)
        elapsed_ms = milliseconds_between(timestamps.get("first_chunk_sent_at"), timestamps.get("last_chunk_sent_at")) if chunks else 0
        metrics["elapsed_audio_send_ms"] = elapsed_ms
        streamed_audio_duration_ms = metrics.get("streamed_audio_duration_ms") or 0
        metrics["realtime_factor"] = round(elapsed_ms / streamed_audio_duration_ms, 3) if streamed_audio_duration_ms else 0

    async def _collect_until_final(self, stt) -> list[STTEvent]:
        events = []
        async for event in stt.events():
            events.append(event)
            if event.is_final:
                break
        return events

    async def _record(self, smoke_test_id: str, event_type: str, payload: dict) -> None:
        event = {"event": event_type, "smoke_test_id": smoke_test_id, **payload, "created_at": isoformat_z(datetime.utcnow())}
        with self.session_factory() as session:
            SmokeTestRepository(session).add_event(smoke_test_id, event_type, event)
        await self.smoke_hub.broadcast(smoke_test_id, event)

    async def _error(self, repo: SmokeTestRepository, smoke_test_id: str, error: str, latency_ms: dict) -> None:
        repo.mark_error(smoke_test_id, error, latency_ms=latency_ms, provider_metrics={"error": error})
        event = {"event": "smoke_error", "smoke_test_id": smoke_test_id, "error": error, "created_at": isoformat_z(datetime.utcnow())}
        repo.add_event(smoke_test_id, "smoke_error", event)
        await self.smoke_hub.broadcast(smoke_test_id, event)

    async def _create_temporary_lesson(self, smoke_test_id: str, target_languages: list[str]) -> str:
        meeting = await MockZoomClient().create_meeting("Temporary Smoke Lesson")
        lesson = Lesson(
            lesson_id=f"lesson_smoke_{uuid4().hex[:10]}",
            title=f"Smoke test {smoke_test_id}",
            mode="mock",
            status="created",
            zoom_meeting_id=meeting.meeting_id,
            zoom_meeting_uuid=meeting.meeting_uuid,
            zoom_join_url=meeting.join_url,
            zoom_start_url=meeting.start_url,
            zoom_topic=meeting.topic,
            zoom_created_at=meeting.created_at,
            stt_provider="mock",
            translation_provider="mock",
            target_languages=",".join(target_languages),
            glossary_id=None,
            glossary_enabled=False,
        )
        with self.session_factory() as session:
            LessonRepository(session).create(lesson)
        return lesson.lesson_id

    def _chunks_for_mode(self, lesson_id: str, audio_mode: str, audio_sample_id: str | None) -> list[AudioChunk]:
        if audio_mode == "wav_upload":
            if not audio_sample_id:
                raise RuntimeError("audio_sample_id is required for wav_upload smoke tests.")
            sample_path = Path(self.settings.smoke_temp_dir) / f"{audio_sample_id}.wav"
            sample = chunk_wav_file(sample_path, self.settings.smoke_audio_chunk_ms)
            return [
                AudioChunk(
                    data=data,
                    lesson_id=lesson_id,
                    source="smoke_wav_upload",
                    sample_rate=sample.sample_rate,
                    channels=sample.channels,
                    format="L16",
                )
                for data in sample.chunks
            ]
        phrase = MOCK_PHRASES[0]
        source = "browser_ws" if audio_mode == "direct_ws" else ("smoke_fake_rtms" if audio_mode == "fake_rtms" else "smoke_mock_chunks")
        return [
            AudioChunk(
                data=f"smoke-audio-{index}".encode(),
                lesson_id=lesson_id,
                source=source,
                sample_rate=16000,
                channels=1,
                format="L16",
                metadata={"text": phrase, "sequence": index},
            )
            for index in range(1)
        ]

    def _caption_payload(
        self,
        lesson_id: str,
        stt_name: str,
        translator_name: str,
        audio_mode: str,
        original_text: str,
        translations: dict[str, str],
        timestamps: dict[str, datetime],
        glossary_meta: dict | None = None,
        original_text_raw: str | None = None,
        streaming_mode: str = "realtime_stream",
    ) -> dict:
        audio_injected_at = timestamps["audio_injected_at"]
        first_chunk_sent_at = timestamps.get("first_chunk_sent_at") or audio_injected_at
        last_chunk_sent_at = timestamps.get("last_chunk_sent_at") or first_chunk_sent_at
        audio_send_completed_at = timestamps.get("audio_send_completed_at") or last_chunk_sent_at
        first_partial_at = timestamps.get("stt_first_partial_at")
        stt_final_at = timestamps["stt_final_at"]
        translation_done_at = timestamps["translation_done_at"]
        websocket_sent_at = timestamps["websocket_sent_at"]
        total_reference_at = audio_send_completed_at if streaming_mode == "fast_upload" else first_chunk_sent_at
        latency = {
            "first_partial": milliseconds_between(first_chunk_sent_at, first_partial_at) if first_partial_at else 0,
            "stt_final": milliseconds_between(last_chunk_sent_at, stt_final_at),
            "translation": milliseconds_between(stt_final_at, translation_done_at),
            "total_server": milliseconds_between(total_reference_at, websocket_sent_at),
            "client_receive": 0,
            "ingest_latency_ms": 0,
            "first_partial_latency_ms": milliseconds_between(first_chunk_sent_at, first_partial_at) if first_partial_at else 0,
            "final_latency_ms": milliseconds_between(last_chunk_sent_at, stt_final_at),
            "translation_latency_ms": milliseconds_between(stt_final_at, translation_done_at),
            "total_latency_ms": milliseconds_between(total_reference_at, websocket_sent_at),
            "total_server_latency_ms": milliseconds_between(total_reference_at, websocket_sent_at),
        }
        return {
            "event": "caption",
            "lesson_id": lesson_id,
            "meeting_id": "smoke",
            "provider": {"stt": stt_name, "translator": translator_name},
            "audio_source": audio_mode,
            "pipeline_id": f"smoke_{lesson_id}",
            "source_language": self.settings.source_language,
            "original_text": original_text,
            "original_text_raw": original_text_raw or original_text,
            "original_text_normalized": original_text,
            "translations": translations,
            "glossary": glossary_meta or {"enabled": False, "glossary_id": None, "normalization_changes": [], "postprocess_changes": []},
            "is_partial": False,
            "is_final": True,
            "speaker": {"id": "teacher", "name": "Teacher"},
            "timestamps": {
                "audio_received_at": isoformat_z(audio_injected_at),
                "first_chunk_sent_at": isoformat_z(first_chunk_sent_at),
                "last_chunk_sent_at": isoformat_z(last_chunk_sent_at),
                "audio_send_completed_at": isoformat_z(audio_send_completed_at),
                "stt_result_at": isoformat_z(stt_final_at),
                "translation_done_at": isoformat_z(translation_done_at),
                "websocket_sent_at": isoformat_z(websocket_sent_at),
            },
            "latency_ms": latency,
        }

    def _stt_kwargs(self, provider_name: str) -> dict:
        if provider_name == "mock":
            return {"source_language": self.settings.source_language}
        if provider_name == "elevenlabs":
            return {
                "api_key": self.settings.elevenlabs_api_key,
                "model_id": self.settings.elevenlabs_stt_model,
                "language": self.settings.elevenlabs_stt_language,
                "audio_format": self.settings.elevenlabs_stt_audio_format,
                "sample_rate": self.settings.elevenlabs_stt_sample_rate,
                "commit_strategy": self.settings.elevenlabs_stt_commit_strategy,
                "enable_partials": self.settings.elevenlabs_stt_enable_partials,
                "max_reconnects": self.settings.elevenlabs_stt_max_reconnects,
                "connect_timeout_seconds": self.settings.elevenlabs_stt_connect_timeout_seconds,
                "receive_timeout_seconds": self.settings.elevenlabs_stt_receive_timeout_seconds,
            }
        if provider_name == "azure":
            return {
                "api_key": self.settings.azure_speech_key,
                "region": self.settings.azure_speech_region,
                "language": self.settings.azure_speech_language,
                "sample_rate": self.settings.azure_speech_sample_rate,
                "bits_per_sample": self.settings.azure_speech_bits_per_sample,
                "channels": self.settings.azure_speech_channels,
                "enable_partials": self.settings.azure_speech_enable_partials,
                "initial_silence_timeout_ms": self.settings.azure_speech_initial_silence_timeout_ms,
                "segmentation_silence_timeout_ms": self.settings.azure_speech_segmentation_silence_timeout_ms,
                "profanity": self.settings.azure_speech_profanity,
                "use_phrase_list": self.settings.azure_speech_use_phrase_list,
            }
        if provider_name == "cartesia":
            return {
                "api_key": self.settings.cartesia_api_key,
                "model": self.settings.cartesia_stt_model,
                "language": self.settings.cartesia_stt_language,
                "encoding": self.settings.cartesia_stt_encoding,
                "sample_rate": self.settings.cartesia_stt_sample_rate,
                "enable_partials": self.settings.cartesia_stt_enable_partials,
                "max_reconnects": self.settings.cartesia_stt_max_reconnects,
                "connect_timeout_seconds": self.settings.cartesia_stt_connect_timeout_seconds,
                "receive_timeout_seconds": self.settings.cartesia_stt_receive_timeout_seconds,
                "version": self.settings.cartesia_stt_version,
            }
        if provider_name == "faster_whisper":
            return faster_whisper_provider_kwargs(self.settings)
        return {}

    def _glossary_terms(self, glossary_id: str | None):
        if not glossary_id:
            return []
        with self.session_factory() as session:
            return GlossaryRepository(session).term_data_for_glossary(glossary_id)

    def _translator_kwargs(self, provider_name: str) -> dict:
        if provider_name == "azure":
            return {
                "api_key": self.settings.azure_translator_key,
                "region": self.settings.azure_translator_region,
                "endpoint": self.settings.azure_translator_endpoint,
                "api_version": self.settings.azure_translator_api_version,
            }
        if provider_name == "local":
            return local_translation_provider_kwargs(self.settings)
        return {}

    def _record_usage(
        self,
        stt_provider: str,
        translation_provider: str,
        lesson_id: str,
        smoke_test_id: str,
        comparison_id: str | None,
        audio_duration_seconds: float,
        audio_bytes: int,
        audio_chunks: int,
        source_text: str,
        target_languages: list[str],
        audio_source: str | None = None,
    ) -> None:
        if self.usage_tracker is None:
            return
        self.usage_tracker.record_stt_audio(
            stt_provider,
            lesson_id=lesson_id,
            smoke_test_id=smoke_test_id,
            comparison_id=comparison_id,
            duration_seconds=audio_duration_seconds,
            byte_count=audio_bytes,
            chunks=audio_chunks,
            audio_source=audio_source,
        )
        self.usage_tracker.record_translation(
            translation_provider,
            lesson_id=lesson_id,
            smoke_test_id=smoke_test_id,
            comparison_id=comparison_id,
            source_text=source_text,
            target_languages=target_languages,
        )
        self.usage_tracker.record_caption(
            stt_provider,
            translation_provider,
            lesson_id=lesson_id,
            smoke_test_id=smoke_test_id,
            comparison_id=comparison_id,
            is_final=True,
        )

    @staticmethod
    def _provider_metrics(provider) -> dict:
        keys = [
            "name",
            "audio_chunks_sent",
            "partial_events_received",
            "final_events_received",
            "errors_count",
            "last_error",
            "last_transcript",
            "audio_bytes_sent",
            "no_match_count",
            "canceled_count",
            "session_started_at",
            "session_stopped_at",
            "stt_provider_latency_ms",
            "provider_connected_at",
            "first_audio_chunk_provider_sent_at",
            "last_audio_chunk_provider_sent_at",
            "first_partial_received_at",
            "first_final_received_at",
            "finalize_sent_at",
            "provider_closed_at",
            "translation_requests_count",
            "translation_errors_count",
            "translation_last_error",
            "translation_avg_latency_ms",
        ]
        return {key: getattr(provider, key) for key in keys if hasattr(provider, key)}

    @staticmethod
    def _stt_timeout_seconds(provider_name: str, streaming_mode: str = "realtime_stream", audio_duration_seconds: float = 0.0) -> float:
        if provider_name == "mock":
            return 2.0
        base = 90.0 if streaming_mode == "realtime_stream" else 60.0
        return base + max(0.0, audio_duration_seconds)


def _trim_trailing_silent_chunks(chunks: list[AudioChunk]) -> list[AudioChunk]:
    last_non_silent = -1
    for index, chunk in enumerate(chunks):
        if any(chunk.data):
            last_non_silent = index
    if last_non_silent < 0:
        return chunks
    return chunks[: last_non_silent + 1]
