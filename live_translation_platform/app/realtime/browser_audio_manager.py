import asyncio
import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.repositories import DebugRepository, LessonRepository
from app.realtime.caption_hub import CaptionHub
from app.realtime.metrics import isoformat_z
from app.schemas.browser_audio import BrowserAudioConfig, BrowserAudioStatus, BrowserAudioStatusResponse, BrowserAudioTuning


class BrowserAudioManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        hub: CaptionHub | None = None,
        debug_repo: DebugRepository | None = None,
        enabled: bool = True,
        queue_max_size: int = 200,
        drop_policy: str = "drop_oldest",
        allow_duplicate_teacher: bool = False,
        expected_sample_rate: int = 16000,
        expected_channels: int = 1,
        expected_format: str = "pcm_s16le",
        chunk_ms: int = 100,
        use_audio_worklet: bool = True,
        enable_resample_in_browser: bool = True,
        commit_strategy: str = "vad",
        manual_commit_after_silence_ms: int = 800,
        force_commit_enabled: bool = True,
        partials_for_live: bool = True,
        silence_rms_threshold: float = 0.01,
        max_segment_duration_ms: int = 5000,
        periodic_commit_enabled: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self.hub = hub
        self.debug_repo = debug_repo
        self.enabled = enabled
        self.queue_max_size = max(1, queue_max_size)
        self.drop_policy = drop_policy
        self.allow_duplicate_teacher = allow_duplicate_teacher
        self.expected_sample_rate = expected_sample_rate
        self.expected_channels = expected_channels
        self.expected_format = expected_format
        self.chunk_ms = chunk_ms
        self.use_audio_worklet = use_audio_worklet
        self.enable_resample_in_browser = enable_resample_in_browser
        self.commit_strategy = commit_strategy
        self.manual_commit_after_silence_ms = max(0, manual_commit_after_silence_ms)
        self.force_commit_enabled = force_commit_enabled
        self.partials_for_live = partials_for_live
        self.silence_rms_threshold = max(0.0, silence_rms_threshold)
        self.max_segment_duration_ms = max(0, max_segment_duration_ms)
        self.periodic_commit_enabled = periodic_commit_enabled
        self.queues: dict[str, asyncio.Queue] = {}
        self.connections: dict[str, Any] = {}
        self.connection_ids: dict[str, str] = {}
        self.latest_connection_id: dict[str, str] = {}
        self.statuses: dict[str, BrowserAudioStatus] = {}
        self.metadata: dict[str, dict] = {}
        self.metadata_received_at: dict[str, datetime] = {}
        self.tuning: dict[str, BrowserAudioTuning] = {}
        self.pending_chunk_metadata: dict[str, dict] = {}
        self.first_audio_at: dict[str, datetime] = {}
        self.connected_at: dict[str, datetime] = {}
        self.last_audio_at: dict[str, datetime] = {}
        self.last_error: dict[str, str] = {}
        self.chunks_received: dict[str, int] = {}
        self.chunks_yielded: dict[str, int] = {}
        self.chunks_dropped: dict[str, int] = {}
        self.bytes_received: dict[str, int] = {}
        self._speech_seen: dict[str, bool] = {}
        self._silence_ms: dict[str, int] = {}
        self._silence_commit_sent: dict[str, bool] = {}
        self._segment_duration_ms: dict[str, int] = {}

    @property
    def config(self) -> BrowserAudioConfig:
        return BrowserAudioConfig(
            enabled=self.enabled,
            queue_max_size=self.queue_max_size,
            drop_policy=self.drop_policy,
            allow_duplicate_teacher=self.allow_duplicate_teacher,
            expected_sample_rate=self.expected_sample_rate,
            expected_channels=self.expected_channels,
            expected_format=self.expected_format,
            chunk_ms=self.chunk_ms,
            use_audio_worklet=self.use_audio_worklet,
            enable_resample_in_browser=self.enable_resample_in_browser,
            commit_strategy=self.commit_strategy,
            manual_commit_after_silence_ms=self.manual_commit_after_silence_ms,
            force_commit_enabled=self.force_commit_enabled,
            partials_for_live=self.partials_for_live,
            silence_rms_threshold=self.silence_rms_threshold,
            max_segment_duration_ms=self.max_segment_duration_ms,
            periodic_commit_enabled=self.periodic_commit_enabled,
        )

    @property
    def active_connections(self) -> int:
        return len(self.connections)

    @property
    def chunks_received_total(self) -> int:
        return sum(self.chunks_received.values())

    @property
    def chunks_dropped_total(self) -> int:
        return sum(self.chunks_dropped.values())

    def get_audio_queue(self, lesson_id: str) -> asyncio.Queue:
        return self._ensure_queue(lesson_id)

    def metadata_for_lesson(self, lesson_id: str) -> dict:
        return dict(self.metadata.get(lesson_id, self._default_metadata(lesson_id)))

    def tuning_for_lesson(self, lesson_id: str) -> BrowserAudioTuning:
        return self.tuning.get(lesson_id) or self._default_tuning()

    def update_tuning(self, lesson_id: str, tuning: BrowserAudioTuning) -> BrowserAudioTuning:
        updated = tuning.model_copy(update={"last_updated_at": tuning.last_updated_at or datetime.utcnow()})
        self.tuning[lesson_id] = updated
        metadata = self.metadata_for_lesson(lesson_id)
        metadata.update(_metadata_from_tuning(updated))
        self.metadata[lesson_id] = metadata
        return updated

    def prepare_lesson(self, lesson_id: str) -> None:
        self._ensure_queue(lesson_id)
        if lesson_id not in self.statuses:
            self.statuses[lesson_id] = BrowserAudioStatus.WAITING_FOR_TEACHER
            self._update_lesson(lesson_id, browser_audio_status=BrowserAudioStatus.WAITING_FOR_TEACHER)

    async def connect(self, lesson_id: str, websocket: Any) -> bool:
        if not self.enabled:
            await self._error(lesson_id, "Browser audio ingest is disabled.")
            return False
        existing = self.connections.get(lesson_id)
        if existing is not None and existing is not websocket:
            if not self.allow_duplicate_teacher:
                await self._error(lesson_id, "Duplicate teacher browser audio connection rejected.")
                return False
            close = getattr(existing, "close", None)
            if close is not None:
                result = close(code=1012)
                if result is not None:
                    await result
        now = datetime.utcnow()
        connection_id = f"ws_{id(websocket):x}"
        self.connections[lesson_id] = websocket
        self.connection_ids[lesson_id] = connection_id
        self.latest_connection_id[lesson_id] = connection_id
        self.connected_at[lesson_id] = now
        self.statuses[lesson_id] = BrowserAudioStatus.CONNECTED
        self._update_lesson(
            lesson_id,
            browser_audio_status=BrowserAudioStatus.CONNECTED,
            browser_audio_connected_at=now,
            browser_audio_error=None,
        )
        await self._debug(lesson_id, "browser_audio_connected", "Browser microphone WebSocket connected")
        return True

    async def disconnect(self, lesson_id: str, websocket: Any | None = None) -> None:
        if websocket is None or self.connections.get(lesson_id) is websocket:
            self.connections.pop(lesson_id, None)
            self.connection_ids.pop(lesson_id, None)
            self.metadata_received_at.pop(lesson_id, None)
            self.pending_chunk_metadata.pop(lesson_id, None)
        elif self.connections.get(lesson_id) is not None:
            return
        self.statuses[lesson_id] = BrowserAudioStatus.DISCONNECTED
        self._update_lesson(lesson_id, browser_audio_status=BrowserAudioStatus.DISCONNECTED)
        await self._debug(lesson_id, "browser_audio_disconnected", "Browser microphone WebSocket disconnected")

    async def handle_text(self, lesson_id: str, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError as exc:
            await self._error(lesson_id, f"Invalid browser audio JSON control message: {exc}")
            return
        event = payload.get("event") or payload.get("type")
        if event == "audio_metadata":
            metadata = self._default_metadata(lesson_id)
            metadata.update({key: value for key, value in payload.items() if key != "event"})
            if "client_started_at" in metadata:
                metadata["client_started_at"] = _parse_timestamp(metadata["client_started_at"])
            self.update_tuning(lesson_id, self._tuning_from_payload(lesson_id, metadata))
            metadata.update(_metadata_from_tuning(self.tuning_for_lesson(lesson_id)))
            self.metadata[lesson_id] = metadata
            self.metadata_received_at[lesson_id] = datetime.utcnow()
            self.prepare_lesson(lesson_id)
            await self._debug(lesson_id, "browser_audio_metadata", "Browser audio metadata received", metadata)
            return
        if event == "audio_chunk":
            chunk_meta = {key: value for key, value in payload.items() if key != "event"}
            for key in ("client_sent_at", "mic_client_capture_at", "audio_ws_sent_at"):
                if key in chunk_meta:
                    chunk_meta[key] = _parse_timestamp(chunk_meta[key])
            if "client_sent_at" not in chunk_meta and "audio_ws_sent_at" in chunk_meta:
                chunk_meta["client_sent_at"] = chunk_meta["audio_ws_sent_at"]
            self.pending_chunk_metadata[lesson_id] = chunk_meta
            return
        if event == "force_commit":
            if self.tuning_for_lesson(lesson_id).force_commit_enabled:
                await self.request_commit(lesson_id, reason=payload.get("reason") or "teacher_force_commit")
            else:
                await self._debug(lesson_id, "browser_audio_force_commit_disabled", "Browser audio force commit is disabled")
            return
        if event == "stt_tuning":
            updates = {key: value for key, value in payload.items() if key != "event"}
            self.update_tuning(lesson_id, self._tuning_from_payload(lesson_id, updates))
            metadata = self.metadata_for_lesson(lesson_id)
            metadata.update(updates)
            metadata.update(_metadata_from_tuning(self.tuning_for_lesson(lesson_id)))
            self.metadata[lesson_id] = metadata
            await self._debug(lesson_id, "browser_audio_stt_tuning", "Browser audio STT tuning updated", updates)
            return
        await self._debug(lesson_id, "browser_audio_control", "Browser audio control message received", payload)

    async def handle_binary(self, lesson_id: str, data: bytes) -> bool:
        now = datetime.utcnow()
        self.prepare_lesson(lesson_id)
        tuning = self.tuning_for_lesson(lesson_id)
        metadata = self.metadata_for_lesson(lesson_id)
        pending = self.pending_chunk_metadata.pop(lesson_id, {})
        client_sent_at = pending.get("client_sent_at")
        metadata.update({key: value for key, value in pending.items() if key != "client_sent_at"})
        metadata["server_received_at"] = now
        metadata["audio_server_received_at"] = now
        metadata["rms"] = _pcm16_rms(data)
        metadata["is_silence"] = metadata["rms"] <= tuning.rms_threshold
        if client_sent_at is not None:
            metadata["client_sent_at"] = client_sent_at
        if metadata.get("audio_ws_sent_at") is None and client_sent_at is not None:
            metadata["audio_ws_sent_at"] = client_sent_at
        if metadata.get("mic_client_capture_at") is None and client_sent_at is not None:
            metadata["mic_client_capture_at"] = client_sent_at
        self.chunks_received[lesson_id] = self.chunks_received.get(lesson_id, 0) + 1
        self.bytes_received[lesson_id] = self.bytes_received.get(lesson_id, 0) + len(data)
        self.first_audio_at.setdefault(lesson_id, now)
        self.last_audio_at[lesson_id] = now
        self.statuses[lesson_id] = BrowserAudioStatus.RECEIVING_AUDIO
        self._update_lesson(
            lesson_id,
            browser_audio_status=BrowserAudioStatus.RECEIVING_AUDIO,
            browser_audio_last_chunk_at=now,
            browser_audio_chunks_received=self.chunks_received[lesson_id],
            browser_audio_bytes_received=self.bytes_received[lesson_id],
            browser_audio_error=None,
        )
        queued = self._enqueue(
            lesson_id,
            {
                "kind": "audio",
                "data": data,
                "timestamp": now,
                "server_received_at": now,
                "client_sent_at": client_sent_at,
                "metadata": metadata,
            },
        )
        await self._debug(
            lesson_id,
            "browser_audio_chunk_received" if queued else "browser_audio_dropped",
            "Browser audio chunk received" if queued else "Browser audio chunk dropped by queue policy",
            {
                "bytes": len(data),
                "queue_size": self._ensure_queue(lesson_id).qsize(),
                "sample_rate": metadata.get("sample_rate"),
                "channels": metadata.get("channels"),
                "format": metadata.get("format"),
            },
        )
        await self._maybe_auto_commit_after_silence(lesson_id, metadata)
        await self._maybe_periodic_commit(lesson_id, metadata)
        return queued

    async def request_commit(self, lesson_id: str, reason: str = "teacher_force_commit", segment_duration_ms: int | None = None) -> bool:
        self.prepare_lesson(lesson_id)
        now = datetime.utcnow()
        metadata = self.metadata_for_lesson(lesson_id)
        metadata.update(
            {
                "control": "stt_commit",
                "reason": reason,
                "commit_reason": reason,
                "segment_duration_ms": segment_duration_ms,
                "server_received_at": now,
                "audio_server_received_at": now,
            }
        )
        queued = self._enqueue(
            lesson_id,
            {
                "kind": "stt_commit",
                "timestamp": now,
                "server_received_at": now,
                "metadata": metadata,
            },
        )
        await self._debug(
            lesson_id,
            "browser_audio_force_commit" if queued else "browser_audio_force_commit_dropped",
            "Browser audio STT commit requested" if queued else "Browser audio STT commit dropped by queue policy",
            {"reason": reason, "queue_size": self._ensure_queue(lesson_id).qsize()},
        )
        return queued

    def mark_yielded(self, lesson_id: str) -> None:
        self.chunks_yielded[lesson_id] = self.chunks_yielded.get(lesson_id, 0) + 1

    def get_status(self, lesson_id: str) -> BrowserAudioStatusResponse:
        status = self.statuses.get(lesson_id)
        connection = self.connections.get(lesson_id)
        metadata_received_at = self.metadata_received_at.get(lesson_id) if connection is not None else None
        lesson = None
        if self.session_factory is not None:
            with self.session_factory() as session:
                lesson = LessonRepository(session).get(lesson_id)
        if status is None and lesson is not None:
            status = BrowserAudioStatus(lesson.browser_audio_status or BrowserAudioStatus.NOT_CONNECTED)
        if status is None:
            status = BrowserAudioStatus.NOT_CONNECTED if lesson_id not in self.queues else BrowserAudioStatus.WAITING_FOR_TEACHER
        return BrowserAudioStatusResponse(
            lesson_id=lesson_id,
            status=status,
            connected_at=self.connected_at.get(lesson_id) or getattr(lesson, "browser_audio_connected_at", None),
            last_audio_at=self.last_audio_at.get(lesson_id) or getattr(lesson, "browser_audio_last_chunk_at", None),
            chunks_received=self.chunks_received.get(lesson_id, getattr(lesson, "browser_audio_chunks_received", 0) if lesson else 0),
            chunks_yielded=self.chunks_yielded.get(lesson_id, 0),
            chunks_dropped=self.chunks_dropped.get(lesson_id, getattr(lesson, "browser_audio_chunks_dropped", 0) if lesson else 0),
            bytes_received=self.bytes_received.get(lesson_id, getattr(lesson, "browser_audio_bytes_received", 0) if lesson else 0),
            queue_size=self._ensure_queue(lesson_id).qsize() if lesson_id in self.queues else 0,
            ws_connected=connection is not None,
            has_active_connection=connection is not None,
            active_connection_id=self.connection_ids.get(lesson_id),
            latest_connection_id=self.latest_connection_id.get(lesson_id),
            ws_ready_state=_websocket_state(connection),
            metadata_received=metadata_received_at is not None,
            metadata_received_at=metadata_received_at,
            binary_frames_received=self.chunks_received.get(lesson_id, getattr(lesson, "browser_audio_chunks_received", 0) if lesson else 0),
            last_binary_frame_at=self.last_audio_at.get(lesson_id) or getattr(lesson, "browser_audio_last_chunk_at", None),
            first_audio_at=self.first_audio_at.get(lesson_id),
            last_error=self.last_error.get(lesson_id) or getattr(lesson, "browser_audio_error", None),
            metadata=self.metadata_for_lesson(lesson_id),
            config=self.config,
            tuning=self.tuning_for_lesson(lesson_id),
        )

    def _enqueue(self, lesson_id: str, event: dict) -> bool:
        queue = self._ensure_queue(lesson_id)
        if queue.full():
            self.chunks_dropped[lesson_id] = self.chunks_dropped.get(lesson_id, 0) + 1
            self._update_lesson(lesson_id, browser_audio_chunks_dropped=self.chunks_dropped[lesson_id])
            if self.drop_policy == "drop_oldest":
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            else:
                return False
        queue.put_nowait(event)
        return True

    def _ensure_queue(self, lesson_id: str) -> asyncio.Queue:
        if lesson_id not in self.queues:
            self.queues[lesson_id] = asyncio.Queue(maxsize=self.queue_max_size)
        return self.queues[lesson_id]

    def _default_tuning(self) -> BrowserAudioTuning:
        return BrowserAudioTuning(
            chunk_ms=self.chunk_ms,
            commit_strategy=self.commit_strategy,
            silence_commit_ms=self.manual_commit_after_silence_ms,
            partials_enabled=self.partials_for_live,
            force_commit_enabled=self.force_commit_enabled,
            max_segment_duration_ms=self.max_segment_duration_ms,
            rms_threshold=self.silence_rms_threshold,
            periodic_commit_enabled=self.periodic_commit_enabled,
        )

    def _tuning_from_payload(self, lesson_id: str, payload: dict) -> BrowserAudioTuning:
        current = self.tuning_for_lesson(lesson_id)
        updates: dict[str, Any] = {}
        if "chunk_ms" in payload:
            updates["chunk_ms"] = int(payload["chunk_ms"] or current.chunk_ms)
        if "commit_strategy" in payload:
            updates["commit_strategy"] = str(payload["commit_strategy"])
        if "manual_commit_after_silence_ms" in payload:
            updates["silence_commit_ms"] = int(payload["manual_commit_after_silence_ms"] or 0)
        if "silence_commit_ms" in payload:
            updates["silence_commit_ms"] = int(payload["silence_commit_ms"] or 0)
        if "partials_for_live" in payload:
            updates["partials_enabled"] = bool(payload["partials_for_live"])
        if "partials_enabled" in payload:
            updates["partials_enabled"] = bool(payload["partials_enabled"])
        if "force_commit_enabled" in payload:
            updates["force_commit_enabled"] = bool(payload["force_commit_enabled"])
        if "max_segment_duration_ms" in payload:
            updates["max_segment_duration_ms"] = int(payload["max_segment_duration_ms"] or 0)
        if "silence_rms_threshold" in payload:
            updates["rms_threshold"] = float(payload["silence_rms_threshold"] or 0.0)
        if "rms_threshold" in payload:
            updates["rms_threshold"] = float(payload["rms_threshold"] or 0.0)
        if "periodic_commit_enabled" in payload:
            updates["periodic_commit_enabled"] = bool(payload["periodic_commit_enabled"])
        if "updated_by" in payload:
            updates["updated_by"] = str(payload["updated_by"]) if payload["updated_by"] is not None else None
        return current.model_copy(update=updates)

    def _default_metadata(self, lesson_id: str | None = None) -> dict:
        tuning = self.tuning_for_lesson(lesson_id) if lesson_id else self._default_tuning()
        return {
            "sample_rate": self.expected_sample_rate,
            "channels": self.expected_channels,
            "format": self.expected_format,
            "source": "browser_mic",
            **_metadata_from_tuning(tuning),
        }

    async def _maybe_auto_commit_after_silence(self, lesson_id: str, metadata: dict) -> None:
        tuning = self.tuning_for_lesson(lesson_id)
        if tuning.commit_strategy != "manual" or tuning.silence_commit_ms <= 0:
            return
        chunk_ms = int(metadata.get("chunk_ms") or tuning.chunk_ms or 0)
        if not metadata.get("is_silence"):
            self._speech_seen[lesson_id] = True
            self._silence_ms[lesson_id] = 0
            self._silence_commit_sent[lesson_id] = False
            return
        if not self._speech_seen.get(lesson_id):
            return
        self._silence_ms[lesson_id] = self._silence_ms.get(lesson_id, 0) + chunk_ms
        if self._silence_ms[lesson_id] < tuning.silence_commit_ms:
            return
        if self._silence_commit_sent.get(lesson_id):
            return
        self._silence_commit_sent[lesson_id] = True
        await self.request_commit(lesson_id, reason="silence_timeout", segment_duration_ms=self._segment_duration_ms.get(lesson_id))

    async def _maybe_periodic_commit(self, lesson_id: str, metadata: dict) -> None:
        tuning = self.tuning_for_lesson(lesson_id)
        if not tuning.periodic_commit_enabled or tuning.max_segment_duration_ms <= 0:
            return
        chunk_ms = int(metadata.get("chunk_ms") or tuning.chunk_ms or 0)
        if metadata.get("is_silence"):
            self._segment_duration_ms[lesson_id] = 0
            return
        self._segment_duration_ms[lesson_id] = self._segment_duration_ms.get(lesson_id, 0) + chunk_ms
        if self._segment_duration_ms[lesson_id] < tuning.max_segment_duration_ms:
            return
        segment_duration_ms = self._segment_duration_ms[lesson_id]
        self._segment_duration_ms[lesson_id] = 0
        await self.request_commit(lesson_id, reason="max_segment_duration", segment_duration_ms=segment_duration_ms)

    async def _error(self, lesson_id: str, message: str) -> None:
        self.statuses[lesson_id] = BrowserAudioStatus.ERROR
        self.last_error[lesson_id] = message
        self._update_lesson(lesson_id, browser_audio_status=BrowserAudioStatus.ERROR, browser_audio_error=message)
        await self._debug(lesson_id, "browser_audio_error", message, level="error")

    async def _debug(
        self,
        lesson_id: str,
        event: str,
        message: str,
        payload: dict | None = None,
        level: str = "info",
    ) -> None:
        debug_payload = {
            "event": event,
            "lesson_id": lesson_id,
            "level": level,
            "message": message,
            "payload": payload or {},
            "created_at": isoformat_z(datetime.utcnow()),
        }
        debug_payload = _json_safe(debug_payload)
        if self.debug_repo is not None:
            self.debug_repo.save(lesson_id, message, level, debug_payload)
        if self.hub is not None:
            await self.hub.broadcast_debug(lesson_id, debug_payload)

    def _update_lesson(self, lesson_id: str, **fields) -> None:
        if self.session_factory is None:
            return
        with self.session_factory() as session:
            repo = LessonRepository(session)
            lesson = repo.get(lesson_id)
            if lesson is None:
                return
            for key, value in fields.items():
                if hasattr(lesson, key):
                    setattr(lesson, key, value.value if isinstance(value, BrowserAudioStatus) else value)
            lesson.updated_at = datetime.utcnow()
            session.commit()


def _parse_timestamp(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.utcfromtimestamp(timestamp)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _json_safe(value):
    if isinstance(value, datetime):
        return isoformat_z(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _metadata_from_tuning(tuning: BrowserAudioTuning) -> dict:
    return {
        "chunk_ms": tuning.chunk_ms,
        "commit_strategy": tuning.commit_strategy,
        "manual_commit_after_silence_ms": tuning.silence_commit_ms,
        "silence_commit_ms": tuning.silence_commit_ms,
        "force_commit_enabled": tuning.force_commit_enabled,
        "partials_for_live": tuning.partials_enabled,
        "partials_enabled": tuning.partials_enabled,
        "silence_rms_threshold": tuning.rms_threshold,
        "rms_threshold": tuning.rms_threshold,
        "max_segment_duration_ms": tuning.max_segment_duration_ms,
        "periodic_commit_enabled": tuning.periodic_commit_enabled,
    }


def _websocket_state(websocket: Any | None) -> str | None:
    if websocket is None:
        return None
    state = getattr(websocket, "client_state", None)
    if state is None:
        return None
    return getattr(state, "name", str(state)).lower()


def _pcm16_rms(data: bytes) -> float:
    if len(data) < 2:
        return 0.0
    sample_count = len(data) // 2
    if sample_count <= 0:
        return 0.0
    total = 0.0
    for index in range(0, sample_count * 2, 2):
        sample = int.from_bytes(data[index : index + 2], byteorder="little", signed=True)
        normalized = sample / 32768.0
        total += normalized * normalized
    return (total / sample_count) ** 0.5
