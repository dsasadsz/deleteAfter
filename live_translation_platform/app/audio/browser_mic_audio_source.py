from collections.abc import AsyncIterator
from datetime import datetime

from app.audio.base import AudioChunk, AudioSource
from app.realtime.browser_audio_manager import BrowserAudioManager


class BrowserMicAudioSource(AudioSource):
    name = "browser_ws"

    def __init__(self, lesson_id: str, manager: BrowserAudioManager) -> None:
        self.lesson_id = lesson_id
        self.manager = manager
        self.queue = manager.get_audio_queue(lesson_id)
        self._closed = False
        self.chunks_received = 0
        self.chunks_yielded = 0
        self.chunks_dropped = 0
        self.bytes_received = 0
        self.first_audio_at: datetime | None = None
        self.last_audio_at: datetime | None = None

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        while not self._closed:
            event = await self.queue.get()
            if event.get("kind") == "stt_commit":
                metadata = event.get("metadata", {})
                timestamp = event.get("timestamp") or event.get("server_received_at") or datetime.utcnow()
                server_received_at = event.get("server_received_at") or metadata.get("server_received_at") or timestamp
                yield AudioChunk(
                    data=b"",
                    received_at=server_received_at,
                    lesson_id=self.lesson_id,
                    source=self.name,
                    timestamp=timestamp,
                    sample_rate=metadata.get("sample_rate"),
                    channels=metadata.get("channels"),
                    format=metadata.get("format"),
                    speaker_id=metadata.get("speaker_id") or "teacher",
                    metadata=metadata,
                    server_received_at=server_received_at,
                )
                continue
            if event.get("kind") != "audio":
                continue
            data = event.get("data", b"")
            metadata = event.get("metadata", {})
            timestamp = event.get("timestamp") or event.get("server_received_at") or datetime.utcnow()
            client_sent_at = event.get("client_sent_at") or metadata.get("client_sent_at")
            server_received_at = event.get("server_received_at") or metadata.get("server_received_at") or timestamp
            self.chunks_received += 1
            self.chunks_yielded += 1
            self.bytes_received += len(data)
            self.first_audio_at = self.first_audio_at or server_received_at
            self.last_audio_at = server_received_at
            self.manager.mark_yielded(self.lesson_id)
            yield AudioChunk(
                data=data,
                received_at=server_received_at,
                lesson_id=self.lesson_id,
                source=self.name,
                timestamp=timestamp,
                sample_rate=metadata.get("sample_rate"),
                channels=metadata.get("channels"),
                format=metadata.get("format"),
                speaker_id=metadata.get("speaker_id") or "teacher",
                metadata=metadata,
                client_sent_at=client_sent_at,
                server_received_at=server_received_at,
            )

    async def close(self) -> None:
        self._closed = True

    @property
    def queue_size(self) -> int:
        return self.queue.qsize()
