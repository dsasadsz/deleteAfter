from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class AudioChunk:
    data: bytes
    received_at: datetime = field(default_factory=datetime.utcnow)
    lesson_id: str = ""
    source: str = "mock"
    timestamp: datetime | None = None
    sample_rate: int | None = None
    channels: int | None = None
    format: str | None = None
    speaker_id: str | None = None
    metadata: dict = field(default_factory=dict)
    client_sent_at: datetime | None = None
    server_received_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.timestamp is None:
            object.__setattr__(self, "timestamp", self.received_at)
        if self.server_received_at is None:
            object.__setattr__(self, "server_received_at", self.received_at)


class AudioSource(ABC):
    name: str

    @abstractmethod
    async def chunks(self) -> AsyncIterator[AudioChunk]:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass
