import asyncio
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Lesson
from app.db.repositories import DebugRepository, LessonRepository
from app.realtime.caption_hub import CaptionHub
from app.realtime.metrics import isoformat_z
from app.schemas.rtms import RTMSStatus, RTMSStatusResponse
from app.zoom.zoom_rtms_client import RTMSUnavailableError, ZoomRTMSClient
from app.zoom.zoom_webhooks import ZoomWebhookContext


class RTMSManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        hub: CaptionHub,
        debug_repo: DebugRepository,
        enabled: bool,
        client_id: str,
        client_secret: str,
        debug_audio_every_n_chunks: int,
        process_audio: bool,
        process_transcript: bool,
        audio_queue_max_size: int = 200,
        audio_drop_policy: str = "drop_oldest",
    ) -> None:
        self.session_factory = session_factory
        self.hub = hub
        self.debug_repo = debug_repo
        self.enabled = enabled
        self.client_id = client_id
        self.client_secret = client_secret
        self.debug_audio_every_n_chunks = max(1, debug_audio_every_n_chunks)
        self.process_audio = process_audio
        self.process_transcript = process_transcript
        self.audio_queue_max_size = max(1, audio_queue_max_size)
        self.audio_drop_policy = audio_drop_policy
        self.clients: dict[str, ZoomRTMSClient] = {}
        self.queues: dict[str, asyncio.Queue] = {}
        self.audio_queues: dict[str, asyncio.Queue] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def start_lesson(self, lesson: Lesson, webhook_payload: dict | None = None, context: ZoomWebhookContext | None = None) -> RTMSStatusResponse:
        if lesson.lesson_id in self.clients:
            return self.status_from_lesson(lesson)

        now = datetime.utcnow()
        self._ensure_audio_queue(lesson.lesson_id)
        if not self.enabled or not (self.client_id and self.client_secret):
            message = "RTMS disabled or SDK not installed"
            with self.session_factory() as session:
                lesson = LessonRepository(session).update_rtms(
                    lesson.lesson_id,
                    rtms_status=RTMSStatus.NOT_CONFIGURED,
                    rtms_stream_id=(context.rtms_stream_id if context else lesson.rtms_stream_id),
                    rtms_session_id=(context.rtms_session_id if context else lesson.rtms_session_id),
                    rtms_started_at=now,
                    rtms_error=message,
                ) or lesson
            await self._broadcast_status(lesson.lesson_id, RTMSStatus.NOT_CONFIGURED, message)
            return self.status_from_lesson(lesson)

        with self.session_factory() as session:
            repo = LessonRepository(session)
            lesson = repo.update_rtms(
                lesson.lesson_id,
                rtms_status=RTMSStatus.WEBHOOK_RECEIVED if webhook_payload else RTMSStatus.WAITING_FOR_MEETING,
                rtms_stream_id=(context.rtms_stream_id if context else lesson.rtms_stream_id),
                rtms_session_id=(context.rtms_session_id if context else lesson.rtms_session_id),
                rtms_started_at=now,
                rtms_error=None,
            ) or lesson

        queue: asyncio.Queue = asyncio.Queue()
        client = ZoomRTMSClient(
            lesson_id=lesson.lesson_id,
            webhook_payload=webhook_payload or {},
            event_queue=queue,
            enabled=self.enabled and bool(self.client_id and self.client_secret),
        )
        self.clients[lesson.lesson_id] = client
        self.queues[lesson.lesson_id] = queue
        self.tasks[lesson.lesson_id] = asyncio.create_task(self._consume_events(lesson.lesson_id, queue))

        await self._broadcast_status(lesson.lesson_id, RTMSStatus.CONNECTING, "RTMS connection requested")
        try:
            await client.connect_from_webhook()
            with self.session_factory() as session:
                lesson = LessonRepository(session).update_rtms(
                    lesson.lesson_id,
                    rtms_status=RTMSStatus.CONNECTED,
                    rtms_connected_at=datetime.utcnow(),
                    rtms_error=None,
                ) or lesson
            await self._broadcast_status(lesson.lesson_id, RTMSStatus.CONNECTED, "RTMS connected")
        except RTMSUnavailableError as exc:
            message = str(exc)
            with self.session_factory() as session:
                lesson = LessonRepository(session).update_rtms(
                    lesson.lesson_id,
                    rtms_status=RTMSStatus.NOT_CONFIGURED,
                    rtms_error=message,
                ) or lesson
            await self._broadcast_status(lesson.lesson_id, RTMSStatus.NOT_CONFIGURED, message)
        except Exception as exc:
            message = f"RTMS error: {exc}"
            with self.session_factory() as session:
                lesson = LessonRepository(session).update_rtms(
                    lesson.lesson_id,
                    rtms_status=RTMSStatus.ERROR,
                    rtms_error=message,
                ) or lesson
            await self._broadcast_status(lesson.lesson_id, RTMSStatus.ERROR, message, level="error")
        return self.status_from_lesson(lesson)

    def get_audio_queue(self, lesson_id: str) -> asyncio.Queue:
        return self._ensure_audio_queue(lesson_id)

    async def inject_audio(
        self,
        lesson_id: str,
        chunks: int,
        sample_rate: int = 16000,
        channels: int = 1,
        chunk_size: int = 3200,
        audio_format: str = "L16",
    ) -> dict:
        for index in range(chunks):
            await self._handle_audio_event(
                lesson_id,
                {
                    "kind": "audio",
                    "data": bytes(chunk_size),
                    "timestamp": datetime.utcnow(),
                    "metadata": {
                        "sample_rate": sample_rate,
                        "channels": channels,
                        "format": audio_format,
                        "sequence": index,
                    },
                },
            )
        return {"lesson_id": lesson_id, "chunks": chunks, "queue_size": self._ensure_audio_queue(lesson_id).qsize()}

    async def stop_lesson(self, lesson_id: str) -> RTMSStatusResponse | None:
        client = self.clients.pop(lesson_id, None)
        if client:
            await client.close()
        task = self.tasks.pop(lesson_id, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.queues.pop(lesson_id, None)
        self.audio_queues.pop(lesson_id, None)
        with self.session_factory() as session:
            lesson = LessonRepository(session).update_rtms(lesson_id, rtms_status=RTMSStatus.DISCONNECTED)
        if lesson:
            await self._broadcast_status(lesson_id, RTMSStatus.DISCONNECTED, "RTMS disconnected")
            return self.status_from_lesson(lesson)
        return None

    def get_status(self, lesson_id: str) -> RTMSStatusResponse | None:
        with self.session_factory() as session:
            lesson = LessonRepository(session).get(lesson_id)
            return self.status_from_lesson(lesson) if lesson else None

    def status_from_lesson(self, lesson: Lesson) -> RTMSStatusResponse:
        return RTMSStatusResponse(
            lesson_id=lesson.lesson_id,
            rtms_status=lesson.rtms_status,
            rtms_stream_id=lesson.rtms_stream_id,
            rtms_session_id=lesson.rtms_session_id,
            rtms_started_at=_iso(lesson.rtms_started_at),
            rtms_connected_at=_iso(lesson.rtms_connected_at),
            rtms_last_audio_at=_iso(lesson.rtms_last_audio_at),
            rtms_last_transcript_at=_iso(lesson.rtms_last_transcript_at),
            rtms_error=lesson.rtms_error,
            rtms_armed=lesson.rtms_armed,
            rtms_armed_at=_iso(lesson.rtms_armed_at),
            audio_chunks_received=lesson.audio_chunks_received,
            transcript_events_received=lesson.transcript_events_received,
            audio_chunks_dropped=lesson.audio_chunks_dropped,
            audio_queue_size=self.audio_queues.get(lesson.lesson_id).qsize() if lesson.lesson_id in self.audio_queues else 0,
            pipeline_status=lesson.pipeline_status,
            pipeline_audio_source=lesson.pipeline_audio_source,
            pipeline_chunks_processed=lesson.pipeline_chunks_processed,
            stt_events_generated=lesson.stt_events_generated,
            captions_sent=lesson.captions_sent,
            stt_provider_status=lesson.stt_provider_status,
            stt_provider_connected_at=_iso(lesson.stt_provider_connected_at),
            stt_provider_audio_chunks_sent=lesson.stt_provider_audio_chunks_sent,
            stt_provider_partial_events=lesson.stt_provider_partial_events,
            stt_provider_final_events=lesson.stt_provider_final_events,
            stt_provider_last_event_at=_iso(lesson.stt_provider_last_event_at),
            stt_provider_errors_count=lesson.stt_provider_errors_count,
            stt_provider_last_error=lesson.stt_provider_last_error,
            translation_requests_count=lesson.translation_requests_count,
            translation_errors_count=lesson.translation_errors_count,
            translation_last_error=lesson.translation_last_error,
            translation_last_success_at=_iso(lesson.translation_last_success_at),
            translation_avg_latency_ms=lesson.translation_avg_latency_ms,
        )

    async def _consume_events(self, lesson_id: str, queue: asyncio.Queue) -> None:
        while True:
            event = await queue.get()
            kind = event.get("kind")
            timestamp = event.get("timestamp") or datetime.utcnow()
            if kind == "audio":
                await self._handle_audio_event(lesson_id, event)
            elif kind == "transcript" and self.process_transcript:
                with self.session_factory() as session:
                    LessonRepository(session).increment_rtms_transcript(lesson_id, timestamp, RTMSStatus.RECEIVING_TRANSCRIPT)
                await self._broadcast_debug(
                    lesson_id,
                    "rtms_transcript",
                    event.get("text", ""),
                    event.get("metadata", {}),
                )
            elif kind in {"session", "participant"}:
                await self._broadcast_debug(lesson_id, f"rtms_{kind}", f"RTMS {kind} event", event.get("metadata", {}))

    async def _broadcast_status(self, lesson_id: str, status: str, message: str, level: str = "info") -> None:
        await self._broadcast_debug(
            lesson_id,
            "rtms_status",
            message,
            {"status": status},
            level=level,
            status=status,
        )

    async def _broadcast_debug(
        self,
        lesson_id: str,
        event: str,
        message: str,
        payload: dict | None = None,
        level: str = "info",
        status: str | None = None,
    ) -> None:
        debug_payload = {
            "event": event,
            "lesson_id": lesson_id,
            "status": status,
            "level": level,
            "message": message,
            "payload": payload or {},
            "created_at": isoformat_z(datetime.utcnow()),
        }
        self.debug_repo.save(lesson_id, message, level, debug_payload)
        await self.hub.broadcast_debug(lesson_id, debug_payload)

    async def _handle_audio_event(self, lesson_id: str, event: dict) -> None:
        timestamp = event.get("timestamp") or datetime.utcnow()
        with self.session_factory() as session:
            lesson = LessonRepository(session).increment_rtms_audio(lesson_id, timestamp, RTMSStatus.RECEIVING_AUDIO)
        await self._broadcast_debug(lesson_id, "rtms_audio_received", "RTMS audio chunk received", event.get("metadata", {}))
        if self.process_audio:
            queued = self._enqueue_audio(lesson_id, event)
            await self._broadcast_debug(
                lesson_id,
                "rtms_audio_queued" if queued else "rtms_audio_dropped",
                "RTMS audio queued for pipeline" if queued else "RTMS audio dropped by queue policy",
                {"queue_size": self._ensure_audio_queue(lesson_id).qsize()},
            )
        if lesson and lesson.audio_chunks_received % self.debug_audio_every_n_chunks == 0:
            await self._broadcast_debug(lesson_id, "rtms_audio_metadata", "RTMS audio chunk metadata", event.get("metadata", {}))

    def _enqueue_audio(self, lesson_id: str, event: dict) -> bool:
        queue = self._ensure_audio_queue(lesson_id)
        if queue.full():
            if self.audio_drop_policy == "drop_oldest":
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                with self.session_factory() as session:
                    LessonRepository(session).increment_rtms_audio_dropped(lesson_id)
            else:
                with self.session_factory() as session:
                    LessonRepository(session).increment_rtms_audio_dropped(lesson_id)
                return False
        queue.put_nowait(event)
        return True

    def _ensure_audio_queue(self, lesson_id: str) -> asyncio.Queue:
        if lesson_id not in self.audio_queues:
            self.audio_queues[lesson_id] = asyncio.Queue(maxsize=self.audio_queue_max_size)
        return self.audio_queues[lesson_id]


def _iso(value: datetime | None) -> str | None:
    return isoformat_z(value) if value else None
