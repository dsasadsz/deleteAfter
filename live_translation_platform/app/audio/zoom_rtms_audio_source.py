import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

from app.audio.base import AudioChunk, AudioSource


class ZoomRTMSAudioSource(AudioSource):
    name = "zoom_rtms"

    def __init__(self, lesson_id: str, queue: asyncio.Queue) -> None:
        self.lesson_id = lesson_id
        self.queue = queue
        self._closed = False
        self.chunks_received_from_rtms = 0
        self.chunks_yielded_to_pipeline = 0
        self.chunks_dropped = 0

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        while not self._closed:
            event = await self.queue.get()
            if event.get("kind") != "audio":
                continue
            self.chunks_received_from_rtms += 1
            metadata = event.get("metadata", {})
            timestamp = event.get("timestamp") or datetime.utcnow()
            self.chunks_yielded_to_pipeline += 1
            yield AudioChunk(
                data=event.get("data", b""),
                received_at=timestamp,
                lesson_id=self.lesson_id,
                source=self.name,
                timestamp=timestamp,
                sample_rate=metadata.get("sample_rate"),
                channels=metadata.get("channels"),
                format=metadata.get("format"),
                speaker_id=metadata.get("speaker_id"),
                metadata=metadata,
            )

    async def close(self) -> None:
        self._closed = True

    @property
    def queue_size(self) -> int:
        return self.queue.qsize()
