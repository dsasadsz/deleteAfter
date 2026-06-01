import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class STTEvent:
    text: str
    is_partial: bool
    is_final: bool
    language: str
    confidence: float | None
    provider: str
    timestamp: datetime
    speaker_id: str | None = None
    raw: dict | None = None
    audio_received_at: datetime | None = None


class STTProvider(ABC):
    name: str
    supports_commit: bool = False

    @abstractmethod
    async def connect(self) -> None:
        pass

    @abstractmethod
    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        pass

    @abstractmethod
    async def events(self) -> AsyncIterator[STTEvent]:
        pass

    async def commit(self, reason: str | None = None) -> None:
        return None

    @abstractmethod
    async def close(self) -> None:
        pass


class UnsupportedSTTProvider(STTProvider):
    def __init__(self, name: str) -> None:
        self.name = name

    async def connect(self) -> None:
        raise NotImplementedError(f"{self.name} STT adapter is planned for Stage 4.")

    async def send_audio(self, audio_chunk: bytes, metadata: dict | None = None) -> None:
        raise NotImplementedError(f"{self.name} STT adapter is planned for Stage 4.")

    async def events(self) -> AsyncIterator[STTEvent]:
        raise NotImplementedError(f"{self.name} STT adapter is planned for Stage 4.")
        yield

    async def close(self) -> None:
        return None


def create_stt_provider(name: str, **kwargs) -> STTProvider:
    normalized = name.lower()
    if normalized == "mock":
        from app.stt.mock_stt import MockSTT

        return MockSTT(**kwargs)
    if normalized == "elevenlabs":
        from app.stt.elevenlabs_stt import ElevenLabsSTTProvider

        return ElevenLabsSTTProvider(**kwargs)
    if normalized == "azure":
        from app.stt.azure_stt import AzureSTTProvider

        return AzureSTTProvider(**kwargs)
    if normalized == "cartesia":
        from app.stt.cartesia_stt import CartesiaSTTProvider

        return CartesiaSTTProvider(**kwargs)
    if normalized == "faster_whisper":
        from app.stt.faster_whisper_stt import FasterWhisperSTTProvider

        return FasterWhisperSTTProvider(**kwargs)
    raise ValueError(f"Unknown STT provider: {name}")


async def queue_event(queue: asyncio.Queue[STTEvent], event: STTEvent) -> None:
    await queue.put(event)
