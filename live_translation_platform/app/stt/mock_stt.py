import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

from app.stt.base import STTEvent, STTProvider
from app.audio.mock_audio_source import MOCK_PHRASES


class MockSTT(STTProvider):
    name = "mock"

    def __init__(
        self,
        source_language: str = "ru-RU",
        audio_driven: bool = False,
        chunks_per_partial: int = 10,
        chunks_per_final: int = 30,
        min_final_interval_ms: int = 1200,
    ) -> None:
        self.source_language = source_language
        self.audio_driven = audio_driven
        self.chunks_per_partial = max(1, chunks_per_partial)
        self.chunks_per_final = max(self.chunks_per_partial, chunks_per_final)
        self.min_final_interval_ms = max(0, min_final_interval_ms)
        self._queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._connected = False
        self._chunk_count = 0
        self._phrase_index = 0
        self._last_final_at: datetime | None = None

    async def connect(self) -> None:
        self._connected = True

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        if not self._connected:
            await self.connect()
        metadata = metadata or {}
        if self.audio_driven and "text" not in metadata:
            await self._send_audio_driven_event(metadata)
            return
        text = metadata.get("text") or audio_chunk.decode(errors="ignore") or "Mock audio"
        audio_received_at = metadata.get("audio_received_at") or datetime.utcnow()
        partial_text = text[: max(8, int(len(text) * 0.55))].rstrip()
        now = datetime.utcnow()
        await self._queue.put(
            STTEvent(
                text=partial_text,
                is_partial=True,
                is_final=False,
                language=self.source_language,
                confidence=0.82,
                provider=self.name,
                timestamp=now,
                speaker_id="teacher",
                raw=self._raw(metadata, "partial", False),
                audio_received_at=audio_received_at,
            )
        )
        await asyncio.sleep(0.03)
        await self._queue.put(
            STTEvent(
                text=text,
                is_partial=False,
                is_final=True,
                language=self.source_language,
                confidence=0.97,
                provider=self.name,
                timestamp=datetime.utcnow(),
                speaker_id="teacher",
                raw=self._raw(metadata, "final", False),
                audio_received_at=audio_received_at,
            )
        )

    async def _send_audio_driven_event(self, metadata: dict) -> None:
        self._chunk_count += 1
        now = datetime.utcnow()
        phrase = MOCK_PHRASES[self._phrase_index % len(MOCK_PHRASES)]
        audio_received_at = metadata.get("audio_received_at") or now
        should_final = self._chunk_count % self.chunks_per_final == 0 and self._final_interval_elapsed(now)
        should_partial = self._chunk_count % self.chunks_per_partial == 0
        if should_partial and not should_final:
            words = phrase.split()
            partial_len = min(len(words), max(2, int(len(words) * 0.55)))
            await self._queue.put(
                STTEvent(
                    text=" ".join(words[:partial_len]) + "...",
                    is_partial=True,
                    is_final=False,
                    language=self.source_language,
                    confidence=0.8,
                    provider=self.name,
                    timestamp=now,
                    speaker_id=metadata.get("speaker_id") or "teacher",
                    raw=self._raw(metadata, "partial", True),
                    audio_received_at=audio_received_at,
                )
            )
        if should_final:
            await self._queue.put(
                STTEvent(
                    text=phrase,
                    is_partial=False,
                    is_final=True,
                    language=self.source_language,
                    confidence=0.96,
                    provider=self.name,
                    timestamp=datetime.utcnow(),
                    speaker_id=metadata.get("speaker_id") or "teacher",
                    raw=self._raw(metadata, "final", True),
                    audio_received_at=audio_received_at,
                )
            )
            self._phrase_index += 1
            self._last_final_at = now

    def _final_interval_elapsed(self, now: datetime) -> bool:
        if self._last_final_at is None:
            return True
        return (now - self._last_final_at).total_seconds() * 1000 >= self.min_final_interval_ms

    def _raw(self, metadata: dict, stage: str, audio_driven: bool) -> dict:
        audio_received_at = metadata.get("audio_received_at")
        client_audio_sent_at = metadata.get("client_audio_sent_at")
        audio_server_received_at = metadata.get("audio_server_received_at")
        audio_pipeline_received_at = metadata.get("audio_pipeline_received_at")
        return {
            "mock": True,
            "stage": stage,
            "audio_driven": audio_driven,
            "audio_source": metadata.get("source", "mock"),
            "audio": {
                "chunk_timestamp": audio_received_at.isoformat() if hasattr(audio_received_at, "isoformat") else None,
                "client_audio_sent_at": client_audio_sent_at.isoformat() if hasattr(client_audio_sent_at, "isoformat") else None,
                "audio_server_received_at": audio_server_received_at.isoformat() if hasattr(audio_server_received_at, "isoformat") else None,
                "audio_pipeline_received_at": audio_pipeline_received_at.isoformat() if hasattr(audio_pipeline_received_at, "isoformat") else None,
                "sample_rate": metadata.get("sample_rate"),
                "channels": metadata.get("channels"),
                "format": metadata.get("format"),
            },
        }

    async def events(self) -> AsyncIterator[STTEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                break
            yield event

    async def close(self) -> None:
        self._connected = False
        await self._queue.put(None)
