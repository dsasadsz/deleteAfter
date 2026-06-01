import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

from app.audio.base import AudioChunk, AudioSource


MOCK_PHRASES = [
    "Сегодня мы изучим переменные в C#.",
    "Теперь рассмотрим циклы for и while.",
    "Давайте создадим простой массив.",
    "Следующий пример показывает работу функции.",
    "Обратите внимание на тип данных string.",
    "Сейчас я объясню, как работает класс.",
    "В конце урока мы решим практическую задачу.",
]


class MockAudioSource(AudioSource):
    name = "mock"

    def __init__(self, interval_seconds: float = 1.3, max_chunks: int | None = None) -> None:
        self.interval_seconds = interval_seconds
        self.max_chunks = max_chunks
        self._closed = False

    async def chunks(self) -> AsyncIterator[AudioChunk]:
        index = 0
        while not self._closed:
            phrase = MOCK_PHRASES[index % len(MOCK_PHRASES)]
            yield AudioChunk(
                data=f"mock-audio-{index}".encode(),
                received_at=datetime.utcnow(),
                source=self.name,
                metadata={"text": phrase, "sequence": index},
            )
            index += 1
            if self.max_chunks is not None and index >= self.max_chunks:
                break
            await asyncio.sleep(self.interval_seconds)

    async def close(self) -> None:
        self._closed = True
