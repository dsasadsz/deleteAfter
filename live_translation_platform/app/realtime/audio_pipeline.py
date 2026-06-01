import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from app.audio.base import AudioChunk, AudioSource
from app.glossary.normalizer import TranscriptNormalizer
from app.glossary.postprocessor import TranslationPostProcessor
from app.glossary.schemas import GlossaryTermData
from app.realtime.metrics import isoformat_z, milliseconds_between
from app.stt.base import STTEvent, STTProvider
from app.translation.base import TranslationProvider
from app.usage.usage_tracker import pcm_duration_seconds


Publish = Callable[[dict], Awaitable[None]]
Save = Callable[[dict], None]
DebugPublish = Callable[[dict], Awaitable[None] | None]
PipelineEvent = Callable[[str, dict], None]
FINAL_CAPTION_DEDUPE_WINDOW_SECONDS = 5


class AudioPipeline:
    def __init__(
        self,
        lesson_id: str,
        meeting_id: str,
        source: AudioSource,
        stt: STTProvider,
        translator: TranslationProvider,
        target_languages: list[str],
        translate_partials: bool,
        publish: Publish,
        save_caption: Save,
        save_metric: Save,
        publish_debug: DebugPublish,
        on_pipeline_event: PipelineEvent | None = None,
        source_language: str = "ru-RU",
        glossary_terms: list[GlossaryTermData] | None = None,
        glossary_id: str | None = None,
        glossary_enabled: bool = False,
        usage_tracker=None,
        queue_max_size: int = 200,
        drop_policy: str = "drop_oldest",
    ) -> None:
        self.lesson_id = lesson_id
        self.meeting_id = meeting_id
        self.source = source
        self.stt = stt
        self.translator = translator
        self.target_languages = target_languages
        self.translate_partials = translate_partials
        self.publish = publish
        self.save_caption = save_caption
        self.save_metric = save_metric
        self.publish_debug = publish_debug
        self.on_pipeline_event = on_pipeline_event
        self.source_language = source_language
        self.glossary_terms = glossary_terms or []
        self.glossary_id = glossary_id
        self.glossary_enabled = glossary_enabled
        self.usage_tracker = usage_tracker
        self.queue_max_size = max(1, queue_max_size)
        self.drop_policy = drop_policy if drop_policy in {"drop_oldest", "drop_newest"} else "drop_oldest"
        self.normalizer = TranscriptNormalizer()
        self.postprocessor = TranslationPostProcessor()
        self.pipeline_id = f"pipeline_{lesson_id}"
        self.queue: asyncio.Queue[AudioChunk] = asyncio.Queue(maxsize=self.queue_max_size)
        self._tasks: list[asyncio.Task[Any]] = []
        self._running = False
        self._accepting_audio = True
        self.status = "created"
        self.error_classification: str | None = None
        self.pipeline_chunks_dropped = 0
        self.pipeline_backpressure_events = 0
        self._last_commit_reason: str | None = None
        self._last_segment_duration_ms: int | None = None
        self._audio_bytes_sent = 0
        self._audio_chunks_sent = 0
        self._audio_duration_seconds = 0.0
        self._usage_flushed = False
        self._audio_timing_by_received_at: dict[str, dict[str, datetime | None]] = {}
        self._final_caption_sequence = 0
        self._recent_final_caption_keys: dict[str, datetime] = {}

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._accepting_audio = True
        self.status = "starting"
        try:
            await self.stt.connect()
        except Exception as exc:
            await self._handle_stt_failure(exc)
            raise
        self._record("stt_provider_metrics", {"provider": self.stt})
        self.status = "running"
        self._record("running", {"source": self.source.name})
        self._tasks = [
            asyncio.create_task(self._pump_audio(), name=f"{self.lesson_id}-audio"),
            asyncio.create_task(self._pump_stt(), name=f"{self.lesson_id}-stt"),
            asyncio.create_task(self._handle_stt_events(), name=f"{self.lesson_id}-events"),
        ]
        await self._debug("pipeline_started", "Audio pipeline started")

    async def stop(self) -> None:
        self._running = False
        self._accepting_audio = False
        await self.source.close()
        await self.stt.close()
        self._flush_audio_usage()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.status = "stopped"
        self._record("stopped", {"source": self.source.name})
        await self._debug("pipeline_stopped", "Audio pipeline stopped")

    async def _pump_audio(self) -> None:
        try:
            async for chunk in self.source.chunks():
                if not self._running:
                    break
                if not self._accepting_audio:
                    await self._drop_chunk(chunk, "stt_disconnected")
                    continue
                chunk.metadata["audio_pipeline_received_at"] = datetime.utcnow()
                self._record("audio_chunk_received", {"source": chunk.source})
                await self._enqueue_audio_chunk(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._debug("audio_error", str(exc), "error")

    async def _pump_stt(self) -> None:
        try:
            while self._running:
                chunk = await self.queue.get()
                metadata = dict(chunk.metadata)
                metadata["audio_received_at"] = chunk.received_at
                metadata["source"] = chunk.source
                metadata["sample_rate"] = chunk.sample_rate
                metadata["channels"] = chunk.channels
                metadata["format"] = chunk.format
                metadata["speaker_id"] = chunk.speaker_id
                metadata["client_audio_sent_at"] = chunk.client_sent_at or metadata.get("client_sent_at")
                metadata["audio_server_received_at"] = chunk.server_received_at or chunk.received_at
                metadata["audio_pipeline_received_at"] = metadata.get("audio_pipeline_received_at") or datetime.utcnow()
                if metadata.get("control") == "stt_commit":
                    reason = metadata.get("reason")
                    self._last_commit_reason = reason
                    self._last_segment_duration_ms = metadata.get("segment_duration_ms")
                    if getattr(self.stt, "supports_commit", True) is False:
                        await self._debug("stt_commit_unsupported", f"STT provider {self.stt.name} does not support commit.", "warning", {"reason": reason})
                        self._record("stt_commit_unsupported", {"source": chunk.source, "reason": reason})
                        continue
                    await self.stt.commit(reason)
                    self._record("stt_commit_requested", {"source": chunk.source, "reason": reason})
                    continue
                self._remember_audio_timing(metadata)
                await self.stt.send_audio(chunk.data, metadata)
                self._track_audio_usage(chunk.data, metadata)
                self._record("audio_chunk_processed", {"source": chunk.source})
                self._record("stt_provider_metrics", {"provider": self.stt})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_stt_failure(exc)

    async def _handle_stt_events(self) -> None:
        try:
            async for event in self.stt.events():
                if not self._running:
                    break
                self._record("stt_event", {"source": event.raw.get("audio_source") if event.raw else self.source.name})
                self._record("stt_provider_metrics", {"provider": self.stt})
                await self._handle_event(event)
            if self._running:
                await self._handle_stt_failure(ConnectionError("STT event stream disconnected."))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_stt_failure(exc)

    async def _handle_event(self, event: STTEvent) -> None:
        normalization = self.normalizer.normalize(event.text, self.glossary_terms) if self.glossary_enabled else None
        normalized_text = normalization.normalized_text if normalization else event.text
        normalization_changes = normalization.changes if normalization else []
        translation_start = datetime.utcnow()
        translations: dict[str, str] = {}
        postprocess_changes: list[dict] = []
        if event.is_final or self.translate_partials:
            try:
                translations = await self.translator.translate_many(normalized_text, event.language, self.target_languages)
                self._record_translation_usage(normalized_text)
            except Exception as exc:
                translations = {language: f"Translation unavailable for {language}" for language in self.target_languages}
                self._record("provider_error", _provider_error_payload(self.translator.name, exc))
                await self._debug("translation_error", str(exc), "error")
            if self.glossary_enabled:
                postprocessed = self.postprocessor.postprocess(normalized_text, translations, self.glossary_terms)
                translations = postprocessed.translations
                postprocess_changes = postprocessed.changes
            self._record("translation_provider_metrics", {"provider": self.translator})
        translation_done_at = datetime.utcnow()
        websocket_sent_at = datetime.utcnow()
        speaker_id = event.speaker_id or "teacher"
        identity_text = _normalize_caption_identity_text(normalized_text)
        translations_identity = _canonical_translation_identity(translations)
        text_hash = _caption_text_hash(identity_text)
        provider_event_id = _provider_event_id(event.raw)
        sequence = None
        caption_id = None
        if event.is_final:
            dedupe_key = _final_caption_dedupe_key(self.lesson_id, speaker_id, identity_text, translations_identity)
            if self._is_duplicate_final_caption(dedupe_key, event.timestamp):
                await self._debug(
                    "duplicate_final_caption_skipped",
                    "Duplicate final caption skipped.",
                    "debug",
                    {
                        "speaker_id": speaker_id,
                        "text_hash": text_hash,
                        "provider_event_id": provider_event_id,
                        "dedupe_window_seconds": FINAL_CAPTION_DEDUPE_WINDOW_SECONDS,
                    },
                )
                self._record("duplicate_final_caption_skipped", {"source": event.raw.get("audio_source") if event.raw else self.source.name})
                return
            self._remember_final_caption_key(dedupe_key, event.timestamp)
            self._final_caption_sequence += 1
            sequence = self._final_caption_sequence
            caption_id = _caption_id(self.lesson_id, sequence, text_hash)
        audio_received_at = event.audio_received_at or event.timestamp
        audio_timing = self._timing_for_event(audio_received_at)
        client_audio_sent_at = audio_timing.get("client_audio_sent_at")
        mic_client_capture_at = audio_timing.get("mic_client_capture_at") or client_audio_sent_at
        audio_ws_sent_at = audio_timing.get("audio_ws_sent_at") or client_audio_sent_at
        audio_server_received_at = audio_timing.get("audio_server_received_at") or audio_received_at
        audio_pipeline_received_at = audio_timing.get("audio_pipeline_received_at") or audio_received_at
        stt_first_partial_at = event.timestamp if event.is_partial else getattr(self.stt, "first_partial_received_at", None)
        stt_final_at = event.timestamp if event.is_final else getattr(self.stt, "first_final_received_at", None)
        ingest_latency_ms = milliseconds_between(audio_ws_sent_at, audio_server_received_at) if audio_ws_sent_at else None
        stt_latency_ms = milliseconds_between(audio_pipeline_received_at, event.timestamp)
        translation_latency_ms = milliseconds_between(translation_start, translation_done_at)
        total_server_latency_ms = milliseconds_between(audio_server_received_at, websocket_sent_at)
        estimated_end_to_end_latency_ms = (
            milliseconds_between(mic_client_capture_at, websocket_sent_at) if mic_client_capture_at else total_server_latency_ms
        )
        latency = {
            "stt": milliseconds_between(audio_received_at, event.timestamp),
            "translation": translation_latency_ms,
            "total": milliseconds_between(audio_received_at, websocket_sent_at),
            "ingest_latency_ms": ingest_latency_ms,
            "first_partial_latency_ms": milliseconds_between(audio_pipeline_received_at, stt_first_partial_at) if stt_first_partial_at else None,
            "final_latency_ms": milliseconds_between(audio_pipeline_received_at, stt_final_at) if stt_final_at else None,
            "stt_latency_ms": stt_latency_ms,
            "translation_latency_ms": translation_latency_ms,
            "total_latency_ms": estimated_end_to_end_latency_ms,
            "total_server_latency_ms": total_server_latency_ms,
            "estimated_end_to_end_latency_ms": estimated_end_to_end_latency_ms,
        }
        payload = {
            "event": "caption",
            "lesson_id": self.lesson_id,
            "meeting_id": self.meeting_id,
            "provider": {"stt": self.stt.name, "translator": self.translator.name},
            "audio_source": event.raw.get("audio_source", self.source.name) if event.raw else self.source.name,
            "pipeline_id": self.pipeline_id,
            "caption_id": caption_id,
            "segment_id": caption_id,
            "text_hash": text_hash,
            "provider_event_id": provider_event_id,
            "audio": event.raw.get("audio", {}) if event.raw else {},
            "source_language": event.language,
            "original_text": normalized_text,
            "original_text_raw": event.text,
            "original_text_normalized": normalized_text,
            "translations": translations,
            "glossary": {
                "enabled": self.glossary_enabled,
                "glossary_id": self.glossary_id,
                "normalization_changes": normalization_changes,
                "postprocess_changes": postprocess_changes,
            },
            "is_partial": event.is_partial,
            "is_final": event.is_final,
            "speaker": {"id": speaker_id, "name": "Teacher"},
            "timestamps": {
                "mic_client_capture_at": isoformat_z(mic_client_capture_at) if mic_client_capture_at else None,
                "audio_ws_sent_at": isoformat_z(audio_ws_sent_at) if audio_ws_sent_at else None,
                "audio_received_at": isoformat_z(audio_received_at),
                "client_audio_sent_at": isoformat_z(client_audio_sent_at) if client_audio_sent_at else None,
                "audio_server_received_at": isoformat_z(audio_server_received_at),
                "audio_pipeline_received_at": isoformat_z(audio_pipeline_received_at),
                "stt_first_partial_at": isoformat_z(stt_first_partial_at) if stt_first_partial_at else None,
                "stt_final_at": isoformat_z(stt_final_at) if stt_final_at else None,
                "stt_result_at": isoformat_z(event.timestamp),
                "translation_done_at": isoformat_z(translation_done_at),
                "websocket_sent_at": isoformat_z(websocket_sent_at),
                "client_caption_received_at": None,
            },
            "latency_ms": latency,
            "pipeline_queue_size": self.queue.qsize(),
            "dropped_chunks": self.pipeline_chunks_dropped,
            "commit_reason": self._last_commit_reason or "none",
            "segment_duration_ms": self._last_segment_duration_ms,
        }
        if sequence is not None:
            payload["sequence"] = sequence
        payload["audio"].update(
            {
                "pipeline_queue_size": self.queue.qsize(),
                "dropped_chunks": self.pipeline_chunks_dropped,
                "commit_reason": self._last_commit_reason or "none",
                "segment_duration_ms": self._last_segment_duration_ms,
            }
        )
        await self.publish(payload)
        self._record_caption_usage(event.is_final)
        self._record("caption_sent", {"source": payload["audio_source"]})
        if event.is_final:
            self.save_caption(payload)
            self.save_metric(payload)

    def _is_duplicate_final_caption(self, dedupe_key: str, event_timestamp: datetime) -> bool:
        self._prune_recent_final_caption_keys(event_timestamp)
        last_seen_at = self._recent_final_caption_keys.get(dedupe_key)
        if last_seen_at is None:
            return False
        return event_timestamp - last_seen_at <= timedelta(seconds=FINAL_CAPTION_DEDUPE_WINDOW_SECONDS)

    def _remember_final_caption_key(self, dedupe_key: str, event_timestamp: datetime) -> None:
        self._recent_final_caption_keys[dedupe_key] = event_timestamp
        self._prune_recent_final_caption_keys(event_timestamp)

    def _prune_recent_final_caption_keys(self, event_timestamp: datetime) -> None:
        cutoff = event_timestamp - timedelta(seconds=FINAL_CAPTION_DEDUPE_WINDOW_SECONDS)
        for key, seen_at in list(self._recent_final_caption_keys.items()):
            if seen_at < cutoff:
                self._recent_final_caption_keys.pop(key, None)

    async def _enqueue_audio_chunk(self, chunk: AudioChunk) -> bool:
        if not self.queue.full():
            self.queue.put_nowait(chunk)
            return True
        if self.drop_policy == "drop_oldest":
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.queue.put_nowait(chunk)
            await self._drop_chunk(chunk, "queue_full")
            return True
        await self._drop_chunk(chunk, "queue_full")
        return False

    async def _drop_chunk(self, chunk: AudioChunk, reason: str) -> None:
        self.pipeline_chunks_dropped += 1
        self.pipeline_backpressure_events += 1
        payload = {
            "drop_policy": self.drop_policy,
            "reason": reason,
            "queue_size": self.queue.qsize(),
            "queue_max_size": self.queue_max_size,
            "pipeline_chunks_dropped": self.pipeline_chunks_dropped,
            "source": chunk.source,
        }
        self._record("pipeline_backpressure", payload)
        await self._debug("pipeline_backpressure", "Audio pipeline queue backpressure dropped a chunk.", "warning", payload)

    async def _handle_stt_failure(self, exc: Exception) -> None:
        classification = _classify_stt_error(exc)
        self.error_classification = classification
        self.status = "degraded" if classification in {"connection_error", "disconnected"} else "error"
        self._accepting_audio = False
        self._running = False
        if hasattr(self.stt, "last_error"):
            self.stt.last_error = str(exc)
        self._record("provider_error", _provider_error_payload(self.stt.name, exc))
        self._record("stt_disconnected", {"source": self.source.name, "classification": classification, "error": str(exc)})
        self._record("stt_provider_metrics", {"provider": self.stt})
        await self._debug(
            "stt_disconnected",
            f"STT provider stopped receiving audio: {exc}",
            "error",
            {"classification": classification, "provider": self.stt.name},
        )

    async def _debug(self, code: str, message: str, level: str = "info", extra: dict | None = None) -> None:
        payload = {
            "event": "debug",
            "lesson_id": self.lesson_id,
            "code": code,
            "level": level,
            "message": message,
            "payload": extra or {},
            "created_at": isoformat_z(datetime.utcnow()),
        }
        result = self.publish_debug(payload)
        if result is not None:
            await result

    def _record(self, event: str, payload: dict) -> None:
        if self.on_pipeline_event is not None:
            self.on_pipeline_event(event, payload)

    def _track_audio_usage(self, audio_chunk: bytes, metadata: dict) -> None:
        self._audio_bytes_sent += len(audio_chunk)
        self._audio_chunks_sent += 1
        self._audio_duration_seconds += pcm_duration_seconds(
            len(audio_chunk),
            metadata.get("sample_rate"),
            metadata.get("channels"),
            16,
        )

    def _flush_audio_usage(self) -> None:
        if self._usage_flushed or self.usage_tracker is None or not self._audio_chunks_sent:
            return
        self._usage_flushed = True
        self.usage_tracker.record_stt_audio(
            self.stt.name,
            lesson_id=self.lesson_id,
            duration_seconds=self._audio_duration_seconds,
            byte_count=self._audio_bytes_sent,
            chunks=self._audio_chunks_sent,
            audio_source=self.source.name,
        )

    def _record_translation_usage(self, text: str) -> None:
        if self.usage_tracker is None:
            return
        self.usage_tracker.record_translation(
            self.translator.name,
            lesson_id=self.lesson_id,
            source_text=text,
            target_languages=self.target_languages,
        )

    def _record_caption_usage(self, is_final: bool) -> None:
        if self.usage_tracker is None:
            return
        self.usage_tracker.record_caption(
            self.stt.name,
            self.translator.name,
            lesson_id=self.lesson_id,
            is_final=is_final,
        )

    def _remember_audio_timing(self, metadata: dict) -> None:
        audio_received_at = metadata.get("audio_received_at")
        if not isinstance(audio_received_at, datetime):
            return
        key = audio_received_at.isoformat()
        self._audio_timing_by_received_at[key] = {
            "client_audio_sent_at": _as_datetime(metadata.get("client_audio_sent_at")),
            "mic_client_capture_at": _as_datetime(metadata.get("mic_client_capture_at")),
            "audio_ws_sent_at": _as_datetime(metadata.get("audio_ws_sent_at")),
            "audio_server_received_at": _as_datetime(metadata.get("audio_server_received_at")) or audio_received_at,
            "audio_pipeline_received_at": _as_datetime(metadata.get("audio_pipeline_received_at")) or audio_received_at,
        }
        if len(self._audio_timing_by_received_at) > 500:
            for old_key in list(self._audio_timing_by_received_at)[:100]:
                self._audio_timing_by_received_at.pop(old_key, None)

    def _timing_for_event(self, audio_received_at: datetime) -> dict[str, datetime | None]:
        key = audio_received_at.isoformat()
        return self._audio_timing_by_received_at.get(
            key,
            {
                "client_audio_sent_at": None,
                "mic_client_capture_at": None,
                "audio_ws_sent_at": None,
                "audio_server_received_at": audio_received_at,
                "audio_pipeline_received_at": audio_received_at,
            },
        )


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _classify_stt_error(exc: Exception) -> str:
    name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    if "config" in name or "configuration" in message or "missing" in message:
        return "configuration_error"
    if "disconnect" in name or "disconnect" in message or "closed" in message or "close" in message:
        return "disconnected"
    if "connection" in name or "connect" in message or "websocket" in message:
        return "connection_error"
    return "runtime_error"


def _provider_error_payload(provider: str, exc: Exception) -> dict:
    return {"provider": provider, "error": str(exc), "error_class": exc.__class__.__name__}


def _normalize_caption_identity_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _canonical_json(value: dict) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_translation_identity(value: dict) -> str:
    return _canonical_json({language: _normalize_caption_identity_text(text) for language, text in (value or {}).items()})


def _caption_text_hash(identity_text: str) -> str:
    return hashlib.sha256(identity_text.encode("utf-8")).hexdigest()


def _caption_id(lesson_id: str, sequence: int, text_hash: str) -> str:
    return f"{lesson_id}:{sequence}:{text_hash[:16]}"


def _final_caption_dedupe_key(lesson_id: str, speaker_id: str, identity_text: str, translations_identity: str) -> str:
    material = "\n".join([lesson_id, speaker_id, identity_text, translations_identity])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _provider_event_id(raw: dict | None) -> str | None:
    if not raw:
        return None
    for key in ("event_id", "id", "result_id", "resultId", "utterance_id", "transcript_id"):
        value = raw.get(key)
        if value:
            return str(value)
    nested_audio = raw.get("audio")
    if isinstance(nested_audio, dict):
        for key in ("event_id", "id", "sequence"):
            value = nested_audio.get(key)
            if value is not None:
                return str(value)
    return None
